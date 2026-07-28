from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from bhiksha.domain.exit_state import (
    ExitActionIntent,
    ExitRuntimeState,
    TradeExitPolicySnapshot,
)
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.execution.order_manager import OrderResult, PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.exit_state import SQLiteExitStateRepository
from bhiksha.persistence.sqlite import (
    SQLiteEventRepository,
    SQLiteTradeStateRepository,
)
from bhiksha.risk_envelope_authority import (
    risk_envelope_authorization_fingerprint,
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
        active_plan_id="plan",
        startup_config_id="startup",
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


def test_restart_reconciles_working_ratchet_and_commits_requested_floor(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="ratchet",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="stop_ratchet",
        action_slot="stop_ratchet:v1",
        expected_state_version=1,
        requested_stop_price=3.25,
        requested_floor_r=1 / 3,
        prior_stop_order_id="STOP",
        prior_stop_price=2.0,
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    asyncio.run(exit_repo.bind_action_order("ratchet", broker_order_id="STOP_R"))
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager(
            "NEW",
            {
                "status": "NEW",
                "orderId": "STOP_R",
                "orderSide": "SELL",
                "openCloseIndicator": "CLOSE",
                "type": "STOP",
                "instrument": {"symbol": OPTION},
                "stopPrice": "3.25",
                "quantity": "1",
                "filledQuantity": None,
            },
        ),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.committed_stop_price == 3.25
    assert recovered.locked_floor_r == pytest.approx(1 / 3)
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


def test_shadow_position_never_enters_live_exit_state_recovery_or_protection(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    manager = _WorkingPartialOrderManager(
        {"status": "NEW"},
        status="NEW",
    )
    supervisor, live_position = _supervisor(tmp_path, manager, exit_repo)
    deployment = _deployment()
    deployment = deployment.model_copy(
        update={
            "exit": deployment.exit.model_copy(
                update={"eod_flat": False},
            )
        }
    )
    shadow_position = replace(
        live_position,
        source="shadow",
        order_id="SHADOW_ENTRY",
        stop_order_id=None,
        stop_price=None,
        entry_timestamp=datetime.now(UTC),
    )

    fields, state = asyncio.run(
        supervisor._hydrate_frozen_profile_state(
            deployment,
            shadow_position,
        )
    )
    managed = asyncio.run(
        supervisor.manage_open_position(
            deployment,
            shadow_position,
            dry_run=True,
        )
    )

    assert fields.profile_id == "TEST"
    assert state.seed_entry_premium == 3.0
    assert managed is not None
    assert manager.stop_placements == 0
    assert "T1" not in supervisor._profile_exit_degraded_trades
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = {
            row[0]
            for row in conn.execute("SELECT event_type FROM events").fetchall()
        }
    assert "exit_state_recovery" not in event_types
    assert "exit_state_degraded_protection" not in event_types
    assert "native_exit_blocked_state_degraded" not in event_types


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


def test_restart_accepts_same_fill_with_broker_subcent_precision(tmp_path) -> None:
    """A precise broker fill and its cent-rounded ledger value are one trade."""

    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    db_path = tmp_path / "exit.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_exit_runtime_state "
            "SET seed_entry_premium=0.9798, initial_risk_per_contract=0.24495, "
            "raw_peak_premium=0.9798, recovery_status='STATE_DEGRADED', "
            "degraded_reason='runtime_state_identity_mismatch', state_version=2 "
            "WHERE trade_id='T1'"
        )
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("NEW", {"status": "NEW"}),
        exit_repo,
    )
    position = replace(position, entry_price=0.98)

    _, profile_state = asyncio.run(
        supervisor._hydrate_frozen_profile_state(_deployment(), position)
    )

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "active"
    assert recovered.degraded_reason is None
    assert recovered.state_version == 3
    assert recovered.seed_entry_premium == pytest.approx(0.9798)
    assert profile_state.seed_entry_premium == pytest.approx(0.9798)
    with sqlite3.connect(tmp_path / "events.db") as conn:
        degraded = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE event_type='exit_state_recovery'"
        ).fetchone()[0]
    assert degraded == 0


def test_restart_rejects_gross_entry_premium_contradiction(tmp_path) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("NEW", {"status": "NEW"}),
        exit_repo,
    )
    position = replace(position, entry_price=4.0)

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.degraded_reason == "runtime_state_identity_mismatch"


def test_restart_rejects_stale_low_premium_post_partial_state(tmp_path) -> None:
    """A 24-cent difference can be a new fill when the option is inexpensive."""

    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    db_path = tmp_path / "exit.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE trade_exit_runtime_state "
            "SET seed_entry_premium=0.10, seed_quantity=2, "
            "initial_risk_per_contract=0.025, raw_peak_premium=0.50, "
            "confirmed_peak_r=16.0, target_1_banked=1, banked_quantity=1, "
            "breakeven_emitted=1, runner_state='post_t1', "
            "recovery_status='STATE_DEGRADED', "
            "degraded_reason='prior_recovery_failure', state_version=2 "
            "WHERE trade_id='T1'"
        )
    manager = _WorkingPartialOrderManager(
        {"status": "NEW"},
        status="NEW",
    )
    supervisor, position = _supervisor(
        tmp_path,
        manager,
        exit_repo,
        quantity=1,
    )
    position = replace(position, entry_price=0.34, stop_price=0.06)
    supervisor.planner.position_tracker.replace_positions([position])

    managed = asyncio.run(
        supervisor.manage_open_position(
            _deployment(),
            position,
            dry_run=False,
        )
    )

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    profile_state = supervisor._profile_exit_states[
        supervisor._profile_state_key(position)
    ]
    assert managed == position
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.degraded_reason == "runtime_state_identity_mismatch"
    assert profile_state.seed_entry_premium == pytest.approx(0.34)
    assert profile_state.banked_quantity == 0
    assert profile_state.breakeven_emitted is False
    assert manager.stop_placements == 0
    assert manager.close_placements == 0


@pytest.mark.parametrize(
    "tracked_entry_premium",
    [None, 0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_restart_rejects_invalid_tracked_entry_premium(
    tmp_path,
    tracked_entry_premium,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    supervisor, position = _supervisor(
        tmp_path,
        _StatusOrderManager("NEW", {"status": "NEW"}),
        exit_repo,
    )
    position = replace(position, entry_price=tracked_entry_premium)

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.recovery_status == "STATE_DEGRADED"
    assert recovered.degraded_reason == "runtime_state_identity_mismatch"


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


class _RatchetOrderManager(StubOrderManager):
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.placements: list[tuple[str, float, str | None]] = []
        self.close_placements = 0

    async def cancel_order(self, order_id: str):
        assert order_id == "STOP"
        if self.outcome == "cancel_pending":
            return False, "cancel_status_pending"
        return True, None

    async def canonicalize_stop_price(
        self,
        option_symbol: str,
        stop_price: float,
        quantity: int,
    ):
        assert option_symbol == OPTION
        assert quantity == 1
        return (int((stop_price + 1e-9) / 0.05) * 0.05, 0.05)

    async def place_stop_loss_order(
        self,
        option_symbol: str,
        stop_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ):
        assert option_symbol == OPTION
        assert quantity == 1
        self.placements.append((option_symbol, stop_price, order_id))
        if len(self.placements) == 1:
            return OrderResult(order_id="STOP_NEW")
        return OrderResult(order_id="STOP_RESTORE")

    async def get_order_status(self, order_id: str):
        if order_id == "STOP":
            if self.outcome == "restart_prior_dead":
                return (
                    "CANCELED",
                    {
                        "status": "CANCELED",
                        "side": "SELL",
                        "openCloseIndicator": "CLOSE",
                        "type": "STOP",
                        "instrument": {"symbol": OPTION},
                        "quantity": "1",
                        "filledQuantity": "0",
                        "stopPrice": "2.00",
                    },
                    None,
                )
            return (
                "NEW",
                {
                    "status": "NEW",
                    "side": "SELL",
                    "openCloseIndicator": "CLOSE",
                    "type": "STOP",
                    "instrument": {"symbol": OPTION},
                    "quantity": "1",
                    "filledQuantity": "0",
                    "stopPrice": "2.00",
                },
                None,
            )
        if order_id == "STOP_NEW" and self.outcome == "ambiguous":
            return None, None, "order_not_indexed_yet"
        if order_id == "STOP_NEW" and self.outcome in {
            "rejected",
            "rejected_with_fill",
            "rejected_missing_fill",
            "restore_ambiguous",
        }:
            filled = {
                "rejected": "0",
                "rejected_with_fill": "1",
                "rejected_missing_fill": None,
                "restore_ambiguous": "0",
            }[self.outcome]
            return (
                "REJECTED",
                {
                    "status": "REJECTED",
                    "orderId": order_id,
                    "side": "SELL",
                    "openCloseIndicator": "CLOSE",
                    "type": "STOP",
                    "instrument": {"symbol": OPTION},
                    "quantity": "1",
                    "filledQuantity": filled,
                    "averagePrice": (
                        "3.25"
                        if self.outcome == "rejected_with_fill"
                        else None
                    ),
                },
                None,
            )
        if order_id == "STOP_RESTORE" and self.outcome == "restore_ambiguous":
            return None, None, "order_not_indexed_yet"
        if order_id == "STOP_NEW" and self.outcome == "filled":
            return (
                "FILLED",
                {
                    "status": "FILLED",
                    "orderId": order_id,
                    "side": "SELL",
                    "openCloseIndicator": "CLOSE",
                    "type": "STOP",
                    "instrument": {"symbol": OPTION},
                    "quantity": "1",
                    "filledQuantity": "1",
                    "averagePrice": "3.25",
                    "closedAt": datetime.now(UTC).isoformat(),
                },
                None,
            )
        stop_price = self.placements[-1][1]
        return (
            "NEW",
            {
                "status": "NEW",
                "orderId": order_id,
                "side": "SELL",
                "openCloseIndicator": "CLOSE",
                "type": "STOP",
                "instrument": {
                    "symbol": (
                        "SPY260401P00556000"
                        if self.outcome == "wrong_symbol"
                        else OPTION
                    )
                },
                "quantity": "1",
                "filledQuantity": None,
                "stopPrice": f"{stop_price:.2f}",
            },
            None,
        )

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
        return OrderResult(order_id="CLOSE")


class _ManageRecoveryOrderManager(_RatchetOrderManager):
    """Stateful broker truth for a full stale-portfolio manage tick."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.canceled_ids: set[str] = set()
        self.dead_replacement = False
        self.status_reads: list[str] = []

    async def cancel_order(self, order_id: str):
        result = await super().cancel_order(order_id)
        if result[0]:
            self.canceled_ids.add(order_id)
        return result

    async def get_order_status(self, order_id: str):
        self.status_reads.append(order_id)
        if order_id in self.canceled_ids:
            return (
                "CANCELED",
                {
                    "status": "CANCELED",
                    "orderId": order_id,
                    "side": "SELL",
                    "openCloseIndicator": "CLOSE",
                    "type": "STOP",
                    "instrument": {"symbol": OPTION},
                    "quantity": "1",
                    "filledQuantity": "0",
                    "stopPrice": "2.00",
                },
                None,
            )
        if order_id == "STOP_NEW" and self.dead_replacement:
            return (
                "REJECTED",
                {
                    "status": "REJECTED",
                    "orderId": order_id,
                    "side": "SELL",
                    "openCloseIndicator": "CLOSE",
                    "type": "STOP",
                    "instrument": {"symbol": OPTION},
                    "quantity": "1",
                    "filledQuantity": "0",
                },
                None,
            )
        status, payload, error = await super().get_order_status(order_id)
        if order_id == "STOP_NEW" and status == "REJECTED":
            self.dead_replacement = True
        return status, payload, error

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=3.60,
            ask=3.65,
            last=3.62,
            quote_timestamp=datetime.now(UTC).isoformat(),
            quote_timestamp_field="quoteTimestamp",
            outcome="SUCCESS",
        )


def _ratchet_deployment():
    base = _deployment()
    return base.model_copy(
        update={
            "deployment_id": (
                "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
            ),
            "symbol": "IWM",
            "execution": base.execution.model_copy(
                update={
                    "runtime_mode": "live_approval_gated",
                    "shadow_only": False,
                    "dte_min": 4,
                    "dte_max": 7,
                    "dte_fallback_policy": "strict",
                }
            ),
            "risk": base.risk.model_copy(
                update={
                    "max_contracts": 1,
                    "max_trade_premium_usd": 2_000.0,
                }
            ),
            "exit": base.exit.model_copy(
                update={
                    "profile_exit_drives_live": True,
                    "risk_envelope_live_mode": "canary",
                    "risk_envelope_live_candidate_id": "safety_stack",
                    "risk_envelope_live_candidate_overlay_hash": (
                        "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e"
                    ),
                    "risk_envelope_live_authorization_id": "test-auth",
                    "risk_envelope_live_start_at": (
                        datetime(2026, 7, 20, tzinfo=UTC)
                    ),
                    "risk_envelope_live_expires_at": (
                        datetime(2026, 8, 1, tzinfo=UTC)
                    ),
                    "risk_envelope_live_authorized_deployment_id": (
                        "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
                    ),
                    "risk_envelope_live_authorized_symbol": "IWM",
                    "risk_envelope_live_authorized_active_plan_id": "plan",
                    "risk_envelope_live_rollback_action": (
                        "disable_canary_restore_control"
                    ),
                    "risk_envelope_live_max_premium_cap_fraction": 0.20,
                }
            ),
        }
    )


def _ratchet_fixture(tmp_path, *, outcome: str):
    deployment = _ratchet_deployment()
    authority_fingerprint = risk_envelope_authorization_fingerprint(
        active_plan_id="plan",
        deployments=[deployment],
    )
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
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
        provenance={
            "entry_context": {"selected_dte": 5},
            "risk_envelope_live": {
                "mode": "canary",
                "candidate_id": "safety_stack",
                "candidate_overlay_hash": (
                    "9f0542fce8f8f7b04e5636bcf3e6dcfffcde15bbb26c1a5cfa4cb1ea5674252e"
                ),
                "authorization_id": "test-auth",
                "start_at": "2026-07-20T00:00:00+00:00",
                "expires_at": "2026-08-01T00:00:00+00:00",
                "authorized_deployment_id": (
                    "strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3"
                ),
                "authorized_symbol": "IWM",
                "authorized_active_plan_id": "plan",
                "startup_authorization_fingerprint": authority_fingerprint,
                "rollback_action": "disable_canary_restore_control",
                "max_premium_cap_fraction": 0.20,
                "max_quote_age_ms": 2_000,
                "max_spread_pct": 0.15,
            },
        },
    )
    state = ExitRuntimeState(
        trade_id="T1",
        deployment_id=snapshot.deployment_id,
        option_symbol=OPTION,
        policy_hash=POLICY_HASH,
        seed_entry_premium=3.0,
        seed_quantity=1,
        initial_risk_per_contract=0.75,
        raw_peak_premium=3.0,
        confirmed_peak_r=0.0,
        runner_state="pre_t1",
        committed_stop_price=2.0,
        state_version=1,
    )
    asyncio.run(exit_repo.freeze_policy_and_initialize_state(snapshot, state))
    manager = _RatchetOrderManager(outcome)
    supervisor, position = _supervisor(
        tmp_path, manager, exit_repo, quantity=1
    )
    supervisor._frozen_exit_policies["T1"] = snapshot
    supervisor._durable_exit_states["T1"] = state
    now = datetime.now(UTC)
    quote = PublicQuote(
        symbol=OPTION,
        bid=3.60,
        ask=3.65,
        last=3.62,
        quote_timestamp=now.isoformat(),
        quote_timestamp_field="quoteTimestamp",
        outcome="SUCCESS",
    )
    return supervisor, position, deployment, quote, exit_repo, manager


def test_live_safety_stack_ratchet_commits_only_after_working_stop_readback(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="working")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert updated.stop_order_id == "STOP_NEW"
    assert updated.stop_price > 2.0
    assert durable.committed_stop_price == updated.stop_price
    assert durable.locked_floor_r is not None
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert len(manager.placements) == 1


def test_live_safety_stack_ratchet_accepts_proved_public_side_timestamps(
    tmp_path,
) -> None:
    supervisor, position, deployment, _, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="working")
    )
    now = datetime.now(UTC)
    quote = PublicQuote(
        symbol=OPTION,
        bid=3.60,
        ask=3.65,
        last=3.62,
        quote_timestamp=(now - timedelta(milliseconds=150)).isoformat(),
        quote_timestamp_field="bidTimestamp+askTimestamp",
        bid_timestamp=(now - timedelta(milliseconds=100)).isoformat(),
        ask_timestamp=(now - timedelta(milliseconds=150)).isoformat(),
        outcome="SUCCESS",
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment,
            position,
            quote,
            dry_run=False,
        )
    )

    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert updated.stop_order_id == "STOP_NEW"
    assert durable.committed_stop_price == updated.stop_price
    assert len(manager.placements) == 1


@pytest.mark.parametrize(
    ("bid_offset_ms", "ask_offset_ms"),
    [
        (-100, 5_000),
        (-100, -2_001),
    ],
)
def test_live_safety_stack_ratchet_rejects_future_or_stale_public_side(
    tmp_path,
    bid_offset_ms,
    ask_offset_ms,
) -> None:
    supervisor, position, deployment, _, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="working")
    )
    now = datetime.now(UTC)
    bid_at = now + timedelta(milliseconds=bid_offset_ms)
    ask_at = now + timedelta(milliseconds=ask_offset_ms)
    quote = PublicQuote(
        symbol=OPTION,
        bid=3.60,
        ask=3.65,
        last=3.62,
        quote_timestamp=min(bid_at, ask_at).isoformat(),
        quote_timestamp_field="bidTimestamp+askTimestamp",
        bid_timestamp=bid_at.isoformat(),
        ask_timestamp=ask_at.isoformat(),
        outcome="SUCCESS",
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment,
            position,
            quote,
            dry_run=False,
        )
    )

    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert updated == position
    assert durable.committed_stop_price == 2.0
    assert manager.placements == []


def test_live_safety_stack_ratchet_restores_prior_stop_after_explicit_rejection(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="rejected")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert updated.stop_order_id == "STOP_RESTORE"
    assert updated.stop_price == 2.0
    assert durable.committed_stop_price == 2.0
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert len(manager.placements) == 2


def test_live_safety_stack_ratchet_leaves_ambiguous_intent_open_without_duplicate(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="ambiguous")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    second = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, updated, quote, dry_run=False
        )
    )

    intents = asyncio.run(exit_repo.get_open_action_intents("T1"))
    assert updated.stop_order_id is None
    assert second.stop_order_id is None
    assert len(intents) == 1
    assert intents[0].action_kind == "stop_ratchet"
    assert len(manager.placements) == 1


def test_live_safety_stack_ratchet_rejects_working_stop_for_wrong_contract(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="wrong_symbol")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    assert updated.stop_order_id is None
    assert len(asyncio.run(exit_repo.get_open_action_intents("T1"))) == 1
    assert len(manager.placements) == 1


def test_ratchet_cancel_pending_keeps_stage_open_and_claims_no_protection(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="cancel_pending")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    intents = asyncio.run(exit_repo.get_open_action_intents("T1"))
    durable = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert updated is not None
    assert updated.stop_order_id is None
    assert updated.stop_price is None
    assert len(intents) == 1
    assert intents[0].handoff_stage == "prior_stop_cancel_pending"
    assert durable.recovery_status == "STATE_DEGRADED"
    assert manager.placements == []


@pytest.mark.parametrize("outcome", ["filled", "rejected_with_fill"])
def test_ratchet_replacement_fill_never_restores_and_closes_one_contract(
    tmp_path,
    outcome: str,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome=outcome)
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    assert updated is None
    assert len(manager.placements) == 1
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert supervisor.planner.position_tracker.active_positions() == []


def test_ratchet_dead_status_without_explicit_zero_fill_is_ambiguous(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="rejected_missing_fill")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    intents = asyncio.run(exit_repo.get_open_action_intents("T1"))
    assert updated is not None and updated.stop_order_id is None
    assert len(manager.placements) == 1
    assert len(intents) == 1
    assert intents[0].handoff_stage == "replacement_submitted"


def test_ratchet_quote_contract_must_match_exactly(tmp_path) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="working")
    )
    quote.symbol = "SPY260401P00556000"

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    assert updated == position
    assert manager.placements == []
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


def test_ratchet_persists_and_submits_same_broker_tick_price(tmp_path) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="ambiguous")
    )

    asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )

    intent = asyncio.run(exit_repo.get_open_action_intents("T1"))[0]
    submitted_price = manager.placements[0][1]
    assert intent.requested_stop_price == submitted_price
    assert round(submitted_price / 0.05) * 0.05 == pytest.approx(
        submitted_price
    )


def test_restart_inspects_prior_stop_before_submitting_replacement(
    tmp_path,
) -> None:
    exit_repo = SQLiteExitStateRepository(str(tmp_path / "exit.db"))
    _seed(exit_repo)
    intent = ExitActionIntent(
        idempotency_key="restart-ratchet",
        trade_id="T1",
        policy_hash=POLICY_HASH,
        action_kind="stop_ratchet",
        action_slot="stop_ratchet:v1",
        expected_state_version=1,
        requested_quantity=1,
        requested_stop_price=3.25,
        requested_floor_r=1 / 3,
        prior_stop_order_id="STOP",
        prior_stop_price=2.0,
        handoff_stage="prior_stop_cancel_pending",
    )
    asyncio.run(exit_repo.prepare_action_intent(intent))
    manager = _RatchetOrderManager("restart_prior_dead")
    supervisor, position = _supervisor(tmp_path, manager, exit_repo)

    asyncio.run(
        supervisor._reconcile_open_profile_intents(
            _deployment(),
            position,
            [intent],
        )
    )

    assert len(manager.placements) == 1
    assert manager.placements[0][2] == "restart-ratchet"
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []


def test_ambiguous_restore_binds_restore_identity_and_blocks_duplicate(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="restore_ambiguous")
    )

    updated = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    intents = asyncio.run(exit_repo.get_open_action_intents("T1"))

    assert updated is not None and updated.stop_order_id is None
    assert len(intents) == 1
    assert intents[0].handoff_stage == "restore_submitted"
    assert intents[0].restore_order_id == "STOP_RESTORE"
    assert len(manager.placements) == 2


@pytest.mark.parametrize(
    ("initial_outcome", "expected_stop_id", "expected_placements"),
    [
        ("ambiguous", "STOP_NEW", 1),
        ("restore_ambiguous", "STOP_RESTORE", 2),
    ],
)
def test_later_working_ratchet_stop_is_adopted_before_intent_resolves(
    tmp_path,
    initial_outcome: str,
    expected_stop_id: str,
    expected_placements: int,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome=initial_outcome)
    )
    degraded = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    assert degraded is not None and degraded.stop_order_id is None
    assert asyncio.run(
        exit_repo.get_risk_envelope_rollback(deployment.deployment_id)
    ) is not None

    manager.outcome = "working"
    asyncio.run(
        supervisor._hydrate_frozen_profile_state(
            deployment,
            degraded,
        )
    )

    adopted = supervisor.planner.position_tracker.active_positions()[0]
    assert adopted.stop_order_id == expected_stop_id
    assert adopted.stop_price == pytest.approx(
        manager.placements[-1][1]
    )
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert "T1" not in supervisor._profile_exit_degraded_trades

    # The durable rollback latch survives the recovery: keep the adopted
    # broker stop, but do not start a third canary handoff on the next tick.
    again = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, adopted, quote, dry_run=False
        )
    )
    assert again == adopted
    assert len(manager.placements) == expected_placements


@pytest.mark.parametrize(
    ("initial_outcome", "recovered_stop_id", "expected_placements"),
    [
        ("ambiguous", "STOP_NEW", 1),
        ("restore_ambiguous", "STOP_RESTORE", 2),
    ],
)
def test_full_manage_tick_uses_recovered_stop_not_stale_portfolio_snapshot(
    tmp_path,
    initial_outcome: str,
    recovered_stop_id: str,
    expected_placements: int,
) -> None:
    supervisor, position, deployment, quote, exit_repo, _ = (
        _ratchet_fixture(tmp_path, outcome=initial_outcome)
    )
    manager = _ManageRecoveryOrderManager(initial_outcome)
    supervisor.planner.order_manager = manager
    stale_portfolio_position = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    assert (
        stale_portfolio_position is not None
        and stale_portfolio_position.stop_order_id is None
    )
    supervisor.planner.position_tracker.open_position(
        stale_portfolio_position.symbol,
        stale_portfolio_position.deployment_id,
        trade_id=stale_portfolio_position.trade_id,
        option_symbol=stale_portfolio_position.option_symbol,
        quantity=stale_portfolio_position.quantity,
        entry_price=stale_portfolio_position.entry_price,
        underlying_entry_price=(
            stale_portfolio_position.underlying_entry_price
        ),
        entry_timestamp=stale_portfolio_position.entry_timestamp,
        source=stale_portfolio_position.source,
        order_id=stale_portfolio_position.order_id,
        stop_order_id=None,
        stop_price=None,
    )

    manager.outcome = "working"
    # Isolate this regression to recovery + generic protection management.
    # The fixture trade is intentionally historical, so leaving live profile
    # dispatch armed would correctly hard-flat it on today's wall clock.
    manage_deployment = deployment.model_copy(
        update={
            "exit": deployment.exit.model_copy(
                update={"profile_exit_drives_live": False}
            )
        }
    )
    managed = asyncio.run(
        supervisor._manage_open_position_locked(
            manage_deployment,
            stale_portfolio_position,
            dry_run=False,
        )
    )

    assert managed is not None
    assert managed.stop_order_id == recovered_stop_id
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.stop_order_id == recovered_stop_id
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
    assert len(manager.placements) == expected_placements

    broker_truth = {
        order_id: asyncio.run(manager.get_order_status(order_id))[0]
        for order_id in ("STOP", "STOP_NEW", "STOP_RESTORE")
        if order_id == recovered_stop_id
        or order_id == "STOP"
        or (
            recovered_stop_id == "STOP_RESTORE"
            and order_id == "STOP_NEW"
        )
    }
    assert [
        order_id
        for order_id, status in broker_truth.items()
        if status == "NEW"
    ] == [recovered_stop_id]


def test_rollback_latch_blocks_new_entries_after_supervisor_restart(
    tmp_path,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome="ambiguous")
    )
    asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    restarted, _ = _supervisor(tmp_path, manager, exit_repo, quantity=1)
    assert restarted.can_submit_deployment_entry(deployment) is True

    result = asyncio.run(
        restarted.handle_signal(
            deployment,
            SignalDecision(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                timestamp=datetime.now(UTC),
                signal=True,
                reason=["test"],
            ),
            dry_run=False,
        )
    )

    assert result is None
    assert restarted.can_submit_deployment_entry(deployment) is False
    assert len(manager.placements) == 1


@pytest.mark.parametrize(
    ("outcome", "stage", "expected_stop_placements"),
    [
        ("cancel_pending", "prior_stop_cancel_pending", 0),
        ("ambiguous", "replacement_submitted", 1),
        ("restore_ambiguous", "restore_submitted", 2),
    ],
)
def test_hard_flat_never_overlaps_unresolved_ratchet_handoff(
    tmp_path,
    outcome: str,
    stage: str,
    expected_stop_placements: int,
) -> None:
    supervisor, position, deployment, quote, exit_repo, manager = (
        _ratchet_fixture(tmp_path, outcome=outcome)
    )
    degraded = asyncio.run(
        supervisor._apply_live_safety_stack_ratchet(
            deployment, position, quote, dry_run=False
        )
    )
    assert degraded is not None
    intent = asyncio.run(exit_repo.get_open_action_intents("T1"))[0]
    assert intent.handoff_stage == stage

    result = asyncio.run(
        supervisor.close_due_positions(
            {position.deployment_id: deployment},
            now=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
            dry_run=False,
        )
    )

    assert result == []
    assert manager.close_placements == 0
    assert len(manager.placements) == expected_stop_placements
    assert len(asyncio.run(exit_repo.get_open_action_intents("T1"))) == 1
