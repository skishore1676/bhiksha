import asyncio
import sqlite3
from datetime import UTC, datetime

from bhiksha.config.models import AppConfig
from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import ExitMode, SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision, TradePlan
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from bhiksha.state.position_tracker import PositionTracker


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

    async def place_square_off_order(self, option_symbol: str, quantity: int):
        class Result:
            order_id = "CLOSE123"
            error = None
        return Result()

    async def place_close_order(self, option_symbol: str, quantity: int, *, exit_mode: ExitMode, limit_price: float | None = None):
        return await self.place_square_off_order(option_symbol, quantity)

    async def place_target_order(self, option_symbol: str, limit_price: float, quantity: int):
        class Result:
            order_id = "TARGET123"
            error = None
        return Result()

    async def cancel_order(self, order_id: str):
        return True, None

    async def get_order_status(self, order_id: str):
        return "FILLED", {"status": "FILLED"}, None

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

    async def plan_entry(self, *args, **kwargs):
        del args, kwargs
        return None


class RecordingOrderManager(StubOrderManager):
    def __init__(self, *, quote_bid: float = 3.0, cancel_success: bool = True):
        self.quote_bid = quote_bid
        self.cancel_success = cancel_success
        self.calls: list[tuple[str, str | float]] = []

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
        self.calls.append(("place_stop", round(stop_price, 2)))
        return await super().place_stop_loss_order(option_symbol, stop_price, quantity)

    async def place_target_order(self, option_symbol: str, limit_price: float, quantity: int):
        self.calls.append(("place_target", round(limit_price, 2)))
        return await super().place_target_order(option_symbol, limit_price, quantity)

    async def place_close_order(self, option_symbol: str, quantity: int, *, exit_mode: ExitMode, limit_price: float | None = None):
        self.calls.append(
            ("place_close_market" if limit_price is None else "place_close_limit", exit_mode.value if limit_price is None else round(limit_price, 2))
        )
        return await super().place_close_order(option_symbol, quantity, exit_mode=exit_mode, limit_price=limit_price)

    async def cancel_order(self, order_id: str):
        self.calls.append(("cancel", order_id))
        if self.cancel_success:
            return True, None
        return False, "cancel_status_unknown"

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=self.quote_bid,
            ask=self.quote_bid + 0.05,
            last=self.quote_bid + 0.02,
            open_interest=500,
            outcome="SUCCESS",
        )


class RecordingPlanner(StubPlanner):
    def __init__(self, order_manager: RecordingOrderManager):
        self.order_manager = order_manager
        self.position_tracker = PositionTracker()


class RecordingManualStatusWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def mark_signal_triggered(self, deployment, decision):
        self.calls.append(("signal_triggered", deployment.deployment_id))
        return None

    async def mark_entry_blocked(self, deployment, *, event_at, note, trade_id=None):
        del event_at, note, trade_id
        self.calls.append(("entry_blocked", deployment.deployment_id))
        return None

    async def mark_entry_planned(self, deployment, *, plan, mode):
        del plan, mode
        self.calls.append(("entry_planned", deployment.deployment_id))
        return None

    async def mark_exit_submitted(self, deployment, *, plan):
        del plan
        self.calls.append(("exit_submitted", deployment.deployment_id))
        return None

    async def mark_closed(self, deployment, *, trade_id, note, event_at=None):
        del trade_id, note, event_at
        self.calls.append(("closed", deployment.deployment_id))
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
        trade_id="TRADE123",
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
    assert [row[0] for row in rows] == ["entry_fill_check", "lifecycle_transition", "protective_stop_submission"]


def test_execution_supervisor_publishes_lifecycle_transition_event(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    bus = InMemoryEventBus()
    queue = bus.subscribe(TradeLifecycleTransitionEvent)
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        event_bus=bus,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE123",
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

    async def run():
        await supervisor._protect_live_entry(plan, deployment)
        return await queue.get()

    event = asyncio.run(run())

    assert event.deployment_id == deployment.deployment_id
    assert event.new_state == "open_protected"


def test_execution_supervisor_publishes_signal_event_even_when_lifecycle_blocks(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    bus = InMemoryEventBus()
    queue = bus.subscribe(SignalEvaluatedEvent)
    lifecycle_store = TradeLifecycleStore()
    lifecycle_store.begin_entry(
        "QQQ",
        "market_impulse_qqq_short_v1",
        option_symbol="QQQ260330P00558000",
        order_id="ENTRY123",
    )
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        lifecycle_store=lifecycle_store,
        event_bus=bus,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
    )

    async def run():
        plan = await supervisor.handle_signal(deployment, decision, dry_run=False)
        event = await queue.get()
        return plan, event

    plan, event = asyncio.run(run())

    assert plan is None
    assert event.decision.deployment_id == deployment.deployment_id
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["signal_decision", "lifecycle_entry_blocked"]


def test_execution_supervisor_publishes_exit_event(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    bus = InMemoryEventBus()
    queue = bus.subscribe(ExitEvaluatedEvent)
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        event_bus=bus,
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
        exit=False,
        action="hold",
        reason=["regime_still_bearish"],
    )

    async def run():
        plan = await supervisor.handle_exit(deployment, position, decision, dry_run=False)
        event = await queue.get()
        return plan, event

    plan, event = asyncio.run(run())

    assert plan is None
    assert event.decision.reason == ["regime_still_bearish"]


def test_execution_supervisor_blocks_entry_when_lifecycle_is_active(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    lifecycle_store = TradeLifecycleStore()
    lifecycle_store.begin_entry(
        "QQQ",
        "market_impulse_qqq_short_v1",
        option_symbol="QQQ260330P00558000",
        order_id="ENTRY123",
    )
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        lifecycle_store=lifecycle_store,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
    )

    plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))

    assert plan is None
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["signal_decision", "lifecycle_entry_blocked"]


def test_execution_supervisor_updates_manual_sheet_status_for_blocked_entry(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    lifecycle_store = TradeLifecycleStore()
    lifecycle_store.begin_entry(
        "QQQ",
        "market_impulse_qqq_short_v1",
        option_symbol="QQQ260330P00558000",
        order_id="ENTRY123",
    )
    status_writer = RecordingManualStatusWriter()
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        lifecycle_store=lifecycle_store,
        manual_status_writer=status_writer,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
    )

    plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=False))

    assert plan is None
    assert status_writer.calls == [
        ("signal_triggered", deployment.deployment_id),
        ("entry_blocked", deployment.deployment_id),
    ]


def test_execution_supervisor_updates_manual_sheet_status_for_planned_entry(tmp_path, monkeypatch) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    status_writer = RecordingManualStatusWriter()
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        manual_status_writer=status_writer,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
    )

    async def _fake_plan_entry(*args, **kwargs):
        del args, kwargs
        return TradePlan(
            trade_id="TRADE123",
            deployment_id=deployment.deployment_id,
            symbol="QQQ",
            direction=SignalDirection.SHORT,
            option_symbol="QQQ260330P00558000",
            quantity=1,
            estimated_entry_price=2.0,
            risk_reasons=["approved"],
            dry_run=True,
            order_id="DRY_RUN",
            underlying_entry_price=500.0,
            entry_timestamp=decision.timestamp,
        )

    monkeypatch.setattr(supervisor.planner, "plan_entry", _fake_plan_entry)

    plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=True))

    assert plan is not None
    assert status_writer.calls[:2] == [
        ("signal_triggered", deployment.deployment_id),
        ("entry_planned", deployment.deployment_id),
    ]


def test_execution_supervisor_self_disarms_sheet_backed_manual_deployment_after_first_trigger(tmp_path, monkeypatch) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    status_writer = RecordingManualStatusWriter()
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        manual_status_writer=status_writer,
    )
    from bhiksha.config.loader import load_deployments

    base_deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    deployment = base_deployment.model_copy(
        update={
            "source": base_deployment.source.model_copy(
                update={
                    "origin": "active_sheet_manual",
                    "metadata": {"row_index": 4, "sheet_name": "manual_entry"},
                }
            )
        }
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
    )

    async def _fake_plan_entry(*args, **kwargs):
        del args, kwargs
        return TradePlan(
            trade_id="TRADE123",
            deployment_id=deployment.deployment_id,
            symbol="QQQ",
            direction=SignalDirection.SHORT,
            option_symbol="QQQ260330P00558000",
            quantity=1,
            estimated_entry_price=2.0,
            risk_reasons=["approved"],
            dry_run=True,
            order_id="DRY_RUN",
            underlying_entry_price=500.0,
            entry_timestamp=decision.timestamp,
        )

    monkeypatch.setattr(supervisor.planner, "plan_entry", _fake_plan_entry)

    first_plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=True))
    second_plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=True))

    assert first_plan is not None
    assert second_plan is None
    assert supervisor.can_submit_deployment_entry(deployment) is False
    assert status_writer.calls == [
        ("signal_triggered", deployment.deployment_id),
        ("entry_planned", deployment.deployment_id),
    ]


def test_execution_supervisor_blocks_live_entry_when_reconciliation_is_stale(tmp_path, monkeypatch) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    status_writer = RecordingManualStatusWriter()
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        manual_status_writer=status_writer,
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 500.0},
    )

    async def _unexpected_plan_entry(*args, **kwargs):
        raise AssertionError("planner.plan_entry should not run when reconciliation is stale")

    monkeypatch.setattr(supervisor.planner, "plan_entry", _unexpected_plan_entry)

    plan = asyncio.run(
        supervisor.handle_signal(
            deployment,
            decision,
            dry_run=False,
            live_entry_block_reason="reconciliation_too_stale",
        )
    )

    assert plan is not None
    assert plan.quantity == 0
    assert plan.risk_reasons == ["reconciliation_too_stale"]
    assert status_writer.calls[:2] == [
        ("signal_triggered", deployment.deployment_id),
        ("entry_blocked", deployment.deployment_id),
    ]


def test_execution_supervisor_does_not_apply_stale_gate_to_dry_run_or_shadow_entries(tmp_path, monkeypatch) -> None:
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 500.0},
    )
    plans = [
        TradePlan(
            trade_id="TRADE-DRY",
            deployment_id=deployment.deployment_id,
            symbol="QQQ",
            direction=SignalDirection.SHORT,
            option_symbol="QQQ260330P00558000",
            quantity=1,
            estimated_entry_price=2.0,
            risk_reasons=["approved"],
            dry_run=True,
            order_id="DRY_RUN",
            underlying_entry_price=500.0,
            entry_timestamp=decision.timestamp,
        ),
        TradePlan(
            trade_id="TRADE-SHADOW",
            deployment_id=deployment.deployment_id,
            symbol="QQQ",
            direction=SignalDirection.SHORT,
            option_symbol="QQQ260330P00558000",
            quantity=1,
            estimated_entry_price=2.0,
            risk_reasons=["approved"],
            dry_run=True,
            order_id=None,
            underlying_entry_price=500.0,
            entry_timestamp=decision.timestamp,
        ),
    ]
    dry_run_supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=SQLiteEventRepository(str(tmp_path / "dry-run-events.db")),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    shadow_supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=SQLiteEventRepository(str(tmp_path / "shadow-events.db")),
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    async def _dry_run_plan_entry(*args, **kwargs):
        del args, kwargs
        return plans[0]

    async def _shadow_plan_entry(*args, **kwargs):
        del args, kwargs
        return plans[1]

    monkeypatch.setattr(dry_run_supervisor.planner, "plan_entry", _dry_run_plan_entry)
    monkeypatch.setattr(shadow_supervisor.planner, "plan_entry", _shadow_plan_entry)

    dry_run_plan = asyncio.run(
        dry_run_supervisor.handle_signal(
            deployment,
            decision,
            dry_run=True,
            live_entry_block_reason="reconciliation_too_stale",
        )
    )
    shadow_plan = asyncio.run(
        shadow_supervisor.handle_signal(
            deployment,
            decision,
            dry_run=False,
            simulate_only=True,
            live_entry_block_reason="reconciliation_too_stale",
        )
    )

    assert dry_run_plan is not None
    assert dry_run_plan.risk_reasons == ["approved"]
    assert shadow_plan is not None
    assert shadow_plan.risk_reasons == ["approved"]


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
        trade_id="TRADE123",
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
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.exit_order_id == "CLOSE123"
    assert tracked.exit_mode == ExitMode.HARD_FLAT


def test_execution_supervisor_halt_and_flattens_positions(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RecordingOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE123",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="broker_sync",
        stop_order_id="STOP123",
        target_order_id="TARGET123",
    )

    closed = asyncio.run(
        supervisor.halt_and_flatten_positions(
            {deployment.deployment_id: deployment},
            dry_run=False,
        )
    )

    assert len(closed) == 1
    assert closed[0].risk_reasons == ["halt_and_flatten_triggered"]
    assert closed[0].order_id == "CLOSE123"
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.exit_order_id == "CLOSE123"
    assert tracked.exit_mode == ExitMode.EMERGENCY
    assert ("cancel", "STOP123") in order_manager.calls
    assert ("cancel", "TARGET123") in order_manager.calls
    assert ("place_close_market", "emergency") in order_manager.calls


def test_execution_supervisor_halt_and_flattens_pending_entries_by_canceling_them(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RecordingOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE124",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="live_pending",
        order_id="ENTRY999",
    )

    closed = asyncio.run(
        supervisor.halt_and_flatten_positions(
            {deployment.deployment_id: deployment},
            dry_run=False,
        )
    )

    assert len(closed) == 1
    assert closed[0].order_id == "ENTRY999"
    assert supervisor.planner.position_tracker.total_open_positions == 0
    assert ("cancel", "ENTRY999") in order_manager.calls


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
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.exit_order_id == "CLOSE123"
    assert tracked.exit_mode == ExitMode.STRATEGY


def test_execution_supervisor_closes_filled_pending_exit(tmp_path) -> None:
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
        trade_id="TRADE123",
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

    asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    assert len(plans) == 1
    assert plans[0].order_id == "CLOSE123"
    assert supervisor.planner.position_tracker.total_open_positions == 0


def test_execution_supervisor_arms_virtual_profit_target_when_broker_supports_single_exit_order(tmp_path) -> None:
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
        trade_id="TRADE123",
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
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.target_order_id is None
    assert tracked.target_price == 3.35

    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [
        "entry_fill_check",
        "profit_target_armed",
        "lifecycle_transition",
        "protective_stop_submission",
    ]


def test_execution_supervisor_places_profit_target_when_broker_supports_concurrent_exit_orders(tmp_path) -> None:
    class ConcurrentExitOrderManager(StubOrderManager):
        supports_concurrent_exit_orders = True

    class ConcurrentExitPlanner(StubPlanner):
        def __init__(self):
            self.order_manager = ConcurrentExitOrderManager()
            self.position_tracker = PositionTracker()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=ConcurrentExitPlanner(),
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
        trade_id="TRADE123",
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


def test_execution_supervisor_activates_virtual_target_for_public(tmp_path) -> None:
    order_manager = RecordingOrderManager(quote_bid=3.30, cancel_success=True)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
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
                    "target_approach_offset_pct": 0.02,
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
        target_price=3.35,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.stop_order_id is None
    assert managed.target_order_id == "TARGET123"
    assert ("cancel", "STOP123") in order_manager.calls
    assert ("place_target", 3.35) in order_manager.calls


def test_execution_supervisor_virtual_target_activation_allows_ambiguous_cancel(tmp_path) -> None:
    order_manager = RecordingOrderManager(quote_bid=3.30, cancel_success=False)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
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
                    "target_approach_offset_pct": 0.02,
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
        target_price=3.35,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.target_order_id == "TARGET123"
    assert ("cancel", "STOP123") in order_manager.calls
    assert ("place_target", 3.35) in order_manager.calls


def test_execution_supervisor_restores_stop_after_virtual_target_pullback(tmp_path) -> None:
    order_manager = RecordingOrderManager(quote_bid=3.00, cancel_success=True)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
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
                    "target_pullback_restore_progress_pct": 0.8,
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
        stop_order_id=None,
        stop_price=1.1,
        target_order_id="TARGET123",
        target_price=3.35,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.target_order_id is None
    assert managed.stop_order_id == "STOP123"
    assert managed.stop_price == 1.1
    assert ("cancel", "TARGET123") in order_manager.calls
    assert ("place_stop", 1.1) in order_manager.calls
