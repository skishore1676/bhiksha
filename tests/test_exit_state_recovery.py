from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
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
        _StatusOrderManager("NEW", {"status": "NEW", "orderId": "STOP_BE"}),
        exit_repo,
    )

    asyncio.run(supervisor._hydrate_frozen_profile_state(_deployment(), position))

    recovered = asyncio.run(exit_repo.get_runtime_state("T1"))
    assert recovered.breakeven_emitted is True
    assert recovered.committed_stop_price == 3.0
    assert asyncio.run(exit_repo.get_open_action_intents("T1")) == []
