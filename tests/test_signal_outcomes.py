import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from bhiksha.config.models import AppConfig
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan
from bhiksha.state.position_tracker import PositionTracker
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from historical_config import historical_deployment


class StubOrderManager:
    supports_concurrent_exit_orders = False
    allows_exit_submission_before_cancel_confirmation = True

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
        return True, {"status": "FILLED"}, None

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
        class Result:
            order_id = "STOP123"
            error = None
        return Result()

    async def cancel_order(self, order_id: str):
        return True, None

    async def get_order_status(self, order_id: str):
        return "FILLED", {"status": "FILLED"}, None


class PlanStubPlanner:
    def __init__(self, plan: TradePlan | None = None):
        self.order_manager = StubOrderManager()
        self.position_tracker = PositionTracker()
        self.plan = plan

    async def close(self):
        return None

    async def plan_entry(self, *args, **kwargs):
        return self.plan


def _make_decision(deployment_id: str, symbol: str) -> SignalDecision:
    return SignalDecision(
        deployment_id=deployment_id,
        symbol=symbol,
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.LONG,
        reason=["test_signal"],
    )


def test_signal_outcome_shadow_filled(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={"enabled": True, "execution": base.execution.model_copy(update={"shadow_only": True})}
    )
    plan = TradePlan(
        trade_id="TRADE_SHADOW_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=1,
        estimated_entry_price=2.50,
        risk_reasons=["approved"],
        dry_run=False,
        order_id=None,
    )
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan),
        event_repository=repo,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False, simulate_only=True))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "filled"
    assert payload["mode"] == "shadow"
    assert payload["attempted_contract"] == "QQQ260330C00550000"
    assert payload["attempted_quantity"] == 1


def test_signal_outcome_live_pending_and_filled(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={"enabled": True, "execution": base.execution.model_copy(update={"shadow_only": False})}
    )
    plan = TradePlan(
        trade_id="TRADE_LIVE_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=2,
        estimated_entry_price=3.00,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ORDER_LIVE_1",
    )
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome' ORDER BY id").fetchall()
    # First is pending_execution from handle_signal, second is filled from _protect_live_entry
    assert len(rows) == 2
    p1 = json.loads(rows[0][1])
    assert p1["outcome"] == "pending_execution"
    assert p1["mode"] == "live"
    p2 = json.loads(rows[1][1])
    assert p2["outcome"] == "filled"
    assert p2["mode"] == "live"


def test_signal_outcome_existing_position_block(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    deployment = historical_deployment("market_impulse_qqq_short_v1").model_copy(update={"enabled": True})
    lifecycle_store = TradeLifecycleStore()
    lifecycle_store.begin_entry("QQQ", deployment.deployment_id, option_symbol="QQQ260330P00558000", order_id="ENTRY123")
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(),
        event_repository=repo,
        lifecycle_store=lifecycle_store,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "existing_position_block"


def test_signal_outcome_budget_block(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    deployment = historical_deployment("market_impulse_qqq_short_v1").model_copy(update={"enabled": True})
    plan = TradePlan(
        trade_id="TRADE_BUDGET_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=0,
        estimated_entry_price=0.0,
        risk_reasons=["insufficient_budget: needed 500 but max_budget is 200"],
        dry_run=False,
        order_id=None,
    )
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan),
        event_repository=repo,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "budget_block"
    assert "insufficient_budget" in payload["rejection_reasons"][0]


def test_signal_outcome_selection_failure(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    deployment = historical_deployment("market_impulse_qqq_short_v1").model_copy(update={"enabled": True})
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan=None),
        event_repository=repo,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "selection_failure"
    assert "planner_returned_no_plan" in payload["rejection_reasons"]


def test_signal_outcome_risk_block(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    deployment = historical_deployment("market_impulse_qqq_short_v1").model_copy(update={"enabled": True})
    plan = TradePlan(
        trade_id="TRADE_RISK_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=0,
        estimated_entry_price=0.0,
        risk_reasons=["risk_envelope_exceeded: max daily loss reached"],
        dry_run=False,
        order_id=None,
    )
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan),
        event_repository=repo,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "risk_block"


def test_signal_outcome_expired_invalidated(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    deployment = historical_deployment("market_impulse_qqq_short_v1").model_copy(update={"enabled": True})
    plan = TradePlan(
        trade_id="TRADE_EXPIRED_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=0,
        estimated_entry_price=0.0,
        risk_reasons=["entry_window_closed: signal after market cutoff time"],
        dry_run=False,
        order_id=None,
    )
    supervisor = ExecutionSupervisor(
        planner=PlanStubPlanner(plan),
        event_repository=repo,
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["outcome"] == "expired_invalidated"


def test_signal_outcome_no_fill_on_unfilled_closed(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    base = historical_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={"enabled": True, "execution": base.execution.model_copy(update={"shadow_only": False})}
    )
    plan = TradePlan(
        trade_id="TRADE_NOFILL_1",
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        direction=SignalDirection.LONG,
        option_symbol="QQQ260330C00550000",
        quantity=1,
        estimated_entry_price=2.00,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ORDER_DEAD_1",
    )

    class DeadOrderManager(StubOrderManager):
        async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
            return False, {"status": "CANCELLED"}, "CANCELLED"

    class DeadPlanner(PlanStubPlanner):
        def __init__(self, plan):
            super().__init__(plan)
            self.order_manager = DeadOrderManager()

    supervisor = ExecutionSupervisor(
        planner=DeadPlanner(plan),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        record_signal_outcomes=True,
    )

    decision = _make_decision(deployment.deployment_id, deployment.symbol)
    res = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))
    assert res is not None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type = 'signal_outcome' ORDER BY id").fetchall()
    # First was pending_execution on submit, second is no_fill on cancellation/dead order
    assert len(rows) == 2
    p1 = json.loads(rows[0][1])
    assert p1["outcome"] == "pending_execution"
    p2 = json.loads(rows[1][1])
    assert p2["outcome"] == "no_fill"

