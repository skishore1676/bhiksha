from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import sqlite3

from bhiksha.domain.exit_state import (
    ExitActionIntent,
    ExitRuntimeState,
    TradeExitPolicySnapshot,
)
from bhiksha.domain.models import ExitDecision
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.exit_state import SQLiteExitStateRepository
from bhiksha.persistence.sqlite import (
    SQLiteEventRepository,
    SQLiteTradeStateRepository,
)

from test_execution_supervisor import RecordingPlanner, StubOrderManager, _enabled_deployment


POLICY = {
    "policy_schema_version": "exit-policy.v1",
    "policy_id": "exit.test.control.v1",
    "stop_family": "premium_pct",
    "stop_anchor": "filled_option_premium",
    "exit_family": "profile_ladder",
    "target_model": "staged_r",
    "target_r": 2.0,
    "target_1_r": 1.0,
    "target_2_r": 2.0,
    "target_1_quantity": 0.5,
    "initial_stop_pct": 0.25,
    "premium_disaster_stop_pct": 0.30,
    "high_water_giveback_policy": "OFF",
    "parameters": {"no_progress_favorable_floor_r": 0.9},
}
POLICY_HASH = canonical_policy_hash(POLICY)
OPTION = "QQQ260401P00556000"


class _StatusOrderManager(StubOrderManager):
    def __init__(self, status: str, payload: dict) -> None:
        self.status = status
        self.payload = payload

    async def get_order_status(self, order_id: str):
        del order_id
        return self.status, self.payload, None


class _WorkingPartialOrderManager(_StatusOrderManager):
    def __init__(
        self,
        payload: dict,
        *,
        status: str = "NEW",
        cancel_success: bool = False,
    ) -> None:
        super().__init__(status, payload)
        self.stop_placements = 0
        self.close_placements = 0
        self.cancel_success = cancel_success

    async def cancel_order(self, order_id: str):
        del order_id
        return (
            (True, None)
            if self.cancel_success
            else (False, "broker_order_still_working")
        )

    async def place_stop_loss_order(
        self,
        option_symbol: str,
        stop_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ):
        del option_symbol, stop_price, quantity, order_id
        self.stop_placements += 1
        return await super().place_stop_loss_order("", 1.0, 1)

    async def place_close_order(
        self,
        option_symbol: str,
        quantity: int,
        *,
        exit_mode,
        limit_price: float | None = None,
        order_id: str | None = None,
    ):
        del option_symbol, quantity, exit_mode, limit_price, order_id
        self.close_placements += 1
        return await super().place_square_off_order("", 1)


def _deployment():
    base = _enabled_deployment("market_impulse_qqq_short_v1")
    return base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "profile_exit_id": "TEST",
                    "target_1_r": 1.0,
                    "target_2_r": 2.0,
                    "target_1_quantity": 0.5,
                    "initial_stop_pct": 0.25,
                    "premium_disaster_stop_pct": 0.30,
                    "high_water_giveback_policy": "OFF",
                }
            )
        }
    )


def _seed(exit_repo: SQLiteExitStateRepository, *, target_1_banked: bool = False):
    snapshot = TradeExitPolicySnapshot(
        trade_id="T1",
        deployment_id="market_impulse_qqq_short_v1",
        option_symbol=OPTION,
        active_plan_id="plan",
        startup_config_id="startup",
        policy_schema_version="exit-policy.v1",
        policy_id="exit.test.control.v1",
        policy_hash=POLICY_HASH,
        canonical_policy=POLICY,
        frozen_at=datetime(2026, 7, 24, tzinfo=UTC),
    )
    state = ExitRuntimeState(
        trade_id="T1",
        deployment_id=snapshot.deployment_id,
        option_symbol=OPTION,
        policy_hash=POLICY_HASH,
        seed_entry_premium=3.0,
        seed_quantity=2,
        initial_risk_per_contract=0.75,
        raw_peak_premium=4.0,
        confirmed_peak_r=4 / 3,
        target_1_banked=target_1_banked,
        banked_quantity=1 if target_1_banked else 0,
        runner_state="post_t1" if target_1_banked else "pre_t1",
        committed_stop_price=2.0,
        state_version=1,
    )
    asyncio.run(exit_repo.freeze_policy_and_initialize_state(snapshot, state))
    return state


def _supervisor(tmp_path, manager, exit_repo, *, quantity: int = 1):
    state_path = str(tmp_path / "trade.db")
    planner = RecordingPlanner(manager)
    planner.position_tracker.open_position(
        "QQQ",
        "market_impulse_qqq_short_v1",
        trade_id="T1",
        option_symbol=OPTION,
        quantity=quantity,
        entry_price=3.0,
        entry_timestamp=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        source="live_open",
        stop_order_id="STOP",
        stop_price=2.0,
    )
    return ExecutionSupervisor(
        planner=planner,
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=SQLiteTradeStateRepository(state_path),
        exit_state_repository=exit_repo,
    ), planner.position_tracker.active_positions()[0]


def test_restart_reconciles_filled_partial_before_profile_can_resume(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:partial",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(exit_repo.bind_action_order("partial", broker_order_id="CLOSE1"))
    payload = {
        "status": "FILLED",
        "side": "SELL",
        "openCloseIndicator": "CLOSE",
        "instrument": {"symbol": OPTION},
        "filledQuantity": "1",
        "averagePrice": "4.0",
        "closedAt": "2026-07-24T14:10:00+00:00",
    }
    supervisor, position = _supervisor(
        tmp_path, _StatusOrderManager("FILLED", payload), exit_repo
    )

    fields, profile_state = asyncio.run(
        supervisor._hydrate_frozen_profile_state(_deployment(), position)
    )

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert fields.policy_hash == POLICY_HASH
    assert fields.no_progress_favorable_floor_r == 0.9
    assert profile_state.peak_premium == 4.0
    assert recovered.target_1_banked is True
    assert recovered.banked_quantity == 1
    assert recovered.runner_state == "post_t1"
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        committed = json.loads(
            conn.execute(
                "SELECT payload FROM events "
                "WHERE event_type='exit_action_committed'"
            ).fetchone()[0]
        )
    assert committed["policy_schema_version"] == "exit-policy.v1"
    assert committed["policy_id"] == "exit.test.control.v1"
    assert committed["policy_hash"] == POLICY_HASH
    assert committed["idempotency_key"] == "partial"


def test_restart_reconciles_working_breakeven_stop_without_reissuing(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo, target_1_banked=True)
    intent = ExitActionIntent(
        idempotency_key="breakeven",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="stop_to_breakeven",
        action_slot="breakeven:one",
        expected_state_version=1,
        requested_stop_price=3.0,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(exit_repo.bind_action_order("breakeven", broker_order_id="STOP_BE"))
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager(
            "NEW",
            {
                "status": "NEW",
                "orderId": "STOP_BE",
                "orderSide": "SELL",
                "openCloseIndicator": "CLOSE",
                "instrument": {"symbol": OPTION},
                "stopPrice": "3.00",
                "quantity": "1",
                "filledQuantity": None,
            },
        ),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.breakeven_emitted is True
    assert recovered.committed_stop_price == 3.0
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


def test_restart_rejects_wrong_size_breakeven_stop(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo, target_1_banked=True)
    intent = ExitActionIntent(
        idempotency_key="breakeven-wrong-size",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="stop_to_breakeven",
        action_slot="breakeven:wrong-size",
        expected_state_version=1,
        requested_stop_price=3.0,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            "breakeven-wrong-size",
            broker_order_id="STOP_WRONG_SIZE",
        )
    )
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager(
            "NEW",
            {
                "status": "NEW",
                "orderId": "STOP_WRONG_SIZE",
                "orderSide": "SELL",
                "openCloseIndicator": "CLOSE",
                "instrument": {"symbol": OPTION},
                "stopPrice": "3.00",
                "quantity": "2",
                "filledQuantity": None,
            },
        ),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.breakeven_emitted is False
    assert asyncio.run(exit_repo.get_open_action_intents("T1"))


def test_restart_recovers_broker_action_accepted_before_local_bind(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="client-order-id",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:client-order-id",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    payload = {
        "status": "FILLED",
        "orderId": "client-order-id",
        "orderSide": "SELL",
        "openCloseIndicator": "CLOSE",
        "instrument": {"symbol": OPTION},
        "filledQuantity": "1",
        "averagePrice": "4.0",
        "closedAt": "2026-07-24T14:10:00+00:00",
    }
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("FILLED", payload),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.target_1_banked is True
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


def test_restart_missing_policy_snapshot_persists_state_degraded(tmp_path) -> None:
    db_path = tmp_path / "exit.db"
    exit_repo = SQLiteExitStateRepository(str(db_path))
    _seed(exit_repo)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM trade_exit_policy_snapshots WHERE trade_id='T1'")
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("NEW", {"status": "NEW"}),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.degraded_reason == "missing_frozen_policy_or_runtime_state"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event = json.loads(
            conn.execute(
                "SELECT payload FROM events "
                "WHERE event_type='exit_state_recovery'"
            ).fetchone()[0]
        )
    assert event["state_version"] == recovered.state_version


def test_partial_fill_confirmation_waits_for_hydration_to_clear_degraded(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial-confirmed-outside-hydration",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:outside-hydration",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            intent.idempotency_key,
            broker_order_id="CLOSE_CONFIRMED",
        )
    )
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("FILLED", {"status": "FILLED"}),
        exit_repo,
    )
    supervisor._profile_exit_degraded_trades.add("T1")

    asyncio.run(
        supervisor._confirm_partial_action_intent(
            "T1",
            order_id="CLOSE_CONFIRMED",
            confirmed_quantity=1,
            broker_payload={"status": "FILLED"},
        )
    )

    assert "T1" in supervisor._profile_exit_degraded_trades
    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))
    assert "T1" not in supervisor._profile_exit_degraded_trades


def test_restart_contradictory_policy_hash_persists_state_degraded(tmp_path) -> None:
    db_path = tmp_path / "exit.db"
    exit_repo = SQLiteExitStateRepository(str(db_path))
    _seed(exit_repo)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_exit_runtime_state SET policy_hash='contradiction' "
            "WHERE trade_id='T1'"
        )
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("NEW", {"status": "NEW"}),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.degraded_reason == "policy_hash_mismatch"


def test_working_partial_does_not_overlap_with_full_size_restored_stop(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial-working",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:partial-working",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            "partial-working",
            broker_order_id="CLOSE_WORKING",
        )
    )
    manager = _WorkingPartialOrderManager(
        {
            "status": "NEW",
            "orderId": "CLOSE_WORKING",
            "orderSide": "SELL",
            "openCloseIndicator": "CLOSE",
            "instrument": {"symbol": OPTION},
            "filledQuantity": "0",
        }
    )
    supervisor, protected_position = _supervisor(tmp_path, manager, exit_repo)
    position = replace(
        protected_position,
        quantity=2,
        stop_order_id=None,
        stop_price=2.0,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))
    restored = asyncio.run(
        supervisor._retain_or_restore_degraded_protection(
            _deployment(),
            position,
            dry_run=False,
        )
    )

    assert restored.stop_order_id is None
    assert manager.stop_placements == 0
    assert asyncio.run(exit_repo.get_open_action_intents("T1"))


def test_canceled_unfilled_partial_restores_full_proved_stop(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial-canceled",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:partial-canceled",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            "partial-canceled",
            broker_order_id="CLOSE_CANCELED",
        )
    )
    manager = _WorkingPartialOrderManager(
        {"status": "NEW", "orderId": "CLOSE_CANCELED"},
        cancel_success=True,
    )
    supervisor, protected_position = _supervisor(tmp_path, manager, exit_repo)
    position = replace(
        protected_position,
        quantity=2,
        stop_order_id=None,
        stop_price=2.0,
    )

    restored = asyncio.run(
        supervisor._retain_or_restore_degraded_protection(
            _deployment(),
            position,
            dry_run=False,
        )
    )

    assert restored.stop_order_id == "STOP123"
    assert restored.stop_price == 2.0
    assert restored.quantity == 2
    assert manager.stop_placements == 1
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


def test_degraded_trade_still_closes_via_universal_hard_flat(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    manager = _WorkingPartialOrderManager(
        {"status": "NEW"},
        cancel_success=True,
    )
    supervisor, position = _supervisor(tmp_path, manager, exit_repo)
    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))
    supervisor._profile_exit_degraded_trades.add("T1")
    native_decision = ExitDecision(
        deployment_id=position.deployment_id,
        symbol=position.symbol,
        timestamp=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["native_stop_loss"],
        cancel_protection_orders=True,
    )

    blocked = asyncio.run(
        supervisor.handle_exit(
            _deployment(),
            position,
            native_decision,
            dry_run=False,
        )
    )
    plans = asyncio.run(
        supervisor.close_due_positions(
            {position.deployment_id: _deployment()},
            now=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
            dry_run=False,
        )
    )

    assert blocked is None
    assert len(plans) == 1
    assert plans[0].risk_reasons == ["hard_flat_time_reached"]
    assert manager.close_placements == 1


def test_hard_flat_blocks_overlapping_full_close_while_partial_is_working(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial-at-hard-flat",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:hard-flat",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            "partial-at-hard-flat",
            broker_order_id="CLOSE_WORKING",
        )
    )
    manager = _WorkingPartialOrderManager(
        {
            "status": "NEW",
            "orderId": "CLOSE_WORKING",
            "orderSide": "SELL",
            "openCloseIndicator": "CLOSE",
            "instrument": {"symbol": OPTION},
            "filledQuantity": "0",
        },
        cancel_success=False,
    )
    supervisor, position = _supervisor(tmp_path, manager, exit_repo)
    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))
    supervisor._profile_exit_degraded_trades.add("T1")

    plans = asyncio.run(
        supervisor.close_due_positions(
            {position.deployment_id: _deployment()},
            now=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
            dry_run=False,
        )
    )

    assert plans == []
    assert manager.close_placements == 0
    assert asyncio.run(exit_repo.get_open_action_intents("T1"))
    with sqlite3.connect(tmp_path / "events.db") as conn:
        issue = json.loads(
            conn.execute(
                "SELECT payload FROM events "
                "WHERE event_type='runtime_issue' ORDER BY id DESC"
            ).fetchone()[0]
        )
    assert issue["category"] == "hard_flat_partial_scale_confirmation_pending"
    assert issue["policy_schema_version"] == "exit-policy.v1"
    assert issue["policy_id"] == "exit.test.control.v1"
    assert issue["idempotency_key"] is None


def test_hard_flat_keeps_blocking_after_fill_proof_until_quantity_refresh(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="partial-filled-before-hard-flat",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="partial_scale",
        action_slot="target_1:filled-before-hard-flat",
        expected_state_version=1,
        requested_quantity=1,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(
        exit_repo.bind_action_order(
            intent.idempotency_key,
            broker_order_id="CLOSE_FILLED",
        )
    )
    manager = _WorkingPartialOrderManager(
        {
            "status": "FILLED",
            "orderId": "CLOSE_FILLED",
            "orderSide": "SELL",
            "openCloseIndicator": "CLOSE",
            "instrument": {"symbol": OPTION},
            "quantity": "1",
            "filledQuantity": "1",
            "averagePrice": "4.00",
        },
        status="FILLED",
        cancel_success=False,
    )
    supervisor, position = _supervisor(
        tmp_path,
        manager,
        exit_repo,
        quantity=2,
    )
    supervisor._profile_exit_degraded_trades.add("T1")
    deployments = {position.deployment_id: _deployment()}
    hard_flat_at = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)

    first = asyncio.run(
        supervisor.close_due_positions(
            deployments,
            now=hard_flat_at,
            dry_run=False,
        )
    )
    second = asyncio.run(
        supervisor.close_due_positions(
            deployments,
            now=hard_flat_at,
            dry_run=False,
        )
    )

    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert first == []
    assert second == []
    assert manager.close_placements == 0
    assert durable.banked_quantity == 1
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert supervisor.planner.position_tracker.active_positions()[0].quantity == 2
