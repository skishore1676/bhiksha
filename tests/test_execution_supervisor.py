import asyncio
import sqlite3
from datetime import UTC, datetime

from bhiksha.config.models import AppConfig
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, TradePlan
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.state.position_tracker import PositionTracker


class StubOrderManager:
    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
        return True, {"status": "FILLED"}, None

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
        class Result:
            order_id = "STOP123"
            error = None
        return Result()

    async def place_square_off_order(self, option_symbol: str, quantity: int):
        class Result:
            order_id = "CLOSE123"
            error = None
        return Result()

    async def place_target_order(self, option_symbol: str, limit_price: float, quantity: int):
        class Result:
            order_id = "TARGET123"
            error = None
        return Result()

    async def cancel_order(self, order_id: str):
        return True, None

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=3.00,
            ask=3.05,
            last=3.02,
            open_interest=500,
            outcome="SUCCESS",
        )


class StubPlanner:
    def __init__(self):
        self.order_manager = StubOrderManager()
        self.position_tracker = PositionTracker()

    async def close(self):
        return None


def test_execution_supervisor_logs_protective_stop(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    plan = TradePlan(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.stop_order_id == "STOP123"
    assert protected.target_order_id is None
    assert supervisor.planner.position_tracker.active_positions()[0].stop_order_id == "STOP123"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["entry_fill_check", "protective_stop_submission"]


def test_execution_supervisor_releases_unfilled_reservation(tmp_path) -> None:
    class UnfilledOrderManager(StubOrderManager):
        async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
            return False, {"status": "CANCELED"}, "CANCELED"

    class UnfilledPlanner(StubPlanner):
        def __init__(self):
            super().__init__()
            self.order_manager = UnfilledOrderManager()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=UnfilledPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="live_pending",
        order_id="ENTRY123",
    )
    plan = TradePlan(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert supervisor.planner.position_tracker.total_open_positions == 0


def test_execution_supervisor_hard_flats_due_positions(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="broker_sync",
    )

    closed = asyncio.run(
        supervisor.close_due_positions(
            {deployment.deployment_id: deployment},
            now=datetime(2026, 3, 30, 20, 56, tzinfo=UTC),
            dry_run=False,
        )
    )

    assert len(closed) == 1
    assert closed[0].order_id == "CLOSE123"
    assert supervisor.planner.position_tracker.total_open_positions == 0


def test_execution_supervisor_handles_algorithmic_exit(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        source="broker_sync",
        stop_order_id="STOP123",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["vma_reclaim_exit"],
        cancel_protection_orders=True,
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is not None
    assert plan.order_id == "CLOSE123"
    assert plan.canceled_stop_order_id == "STOP123"
    assert supervisor.planner.position_tracker.total_open_positions == 0


def test_execution_supervisor_places_profit_target_when_enabled(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    base = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_profit_target": True,
                    "profit_target_multiple": 1.5,
                }
            )
        }
    )
    plan = TradePlan(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.stop_order_id == "STOP123"
    assert protected.target_order_id == "TARGET123"
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.target_order_id == "TARGET123"
    assert tracked.target_price == 3.35


def test_execution_supervisor_promotes_stop_to_breakeven(tmp_path) -> None:
    class BreakevenOrderManager(StubOrderManager):
        async def get_option_quote(self, option_symbol: str):
            return PublicQuote(
                symbol=option_symbol,
                bid=3.0,
                ask=3.05,
                last=3.02,
                open_interest=500,
                outcome="SUCCESS",
            )

    class BreakevenPlanner(StubPlanner):
        def __init__(self):
            super().__init__()
            self.order_manager = BreakevenOrderManager()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=BreakevenPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    base = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "stop_to_breakeven_after_r_multiple": 1.0,
                }
            )
        }
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="broker_sync",
        stop_order_id="STOP123",
        stop_price=1.1,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.stop_order_id == "STOP123"
    assert managed.stop_price == 2.0
