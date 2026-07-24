from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import sqlite3

from bhiksha.domain.exit_state import (
    ExitActionIntent,
    ExitRuntimeState,
    TradeExitPolicySnapshot,
)
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
    def __init__(self, payload: dict) -> None:
        super().__init__("NEW", payload)
        self.stop_placements = 0

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


def _supervisor(tmp_path, manager, exit_repo):
    state_path = str(tmp_path / "trade.db")
    planner = RecordingPlanner(manager)
    planner.position_tracker.open_position(
        "QQQ",
        "market_impulse_qqq_short_v1",
        trade_id="T1",
        option_symbol=OPTION,
        quantity=1,
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
            },
        ),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.breakeven_emitted is True
    assert recovered.committed_stop_price == 3.0
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


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
