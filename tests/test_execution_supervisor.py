import asyncio
import json
import sqlite3
from datetime import UTC, datetime

from bhiksha.config.models import AppConfig
from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import ExitMode, SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.order_manager import OrderResult, PreflightCheck, PublicQuote
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.sqlite import SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from bhiksha.state.position_tracker import PositionTracker
from historical_config import historical_deployment


def _enabled_deployment(deployment_id: str):
    deployment = historical_deployment(deployment_id)
    return deployment.model_copy(update={"enabled": True})


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


class RecordingCashGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def finalize_entry(self, trade_id: str) -> None:
        self.calls.append(("finalize", trade_id))

    async def release_entry(self, trade_id: str) -> None:
        self.calls.append(("release", trade_id))

    async def sync_positions(self, positions, trades) -> None:
        del positions, trades
        return None


class RecordingOrderManager(StubOrderManager):
    def __init__(self, *, quote_bid: float = 3.0, cancel_success: bool = True, portfolio: dict | None = None):
        self.quote_bid = quote_bid
        self.cancel_success = cancel_success
        self.portfolio = portfolio or {}
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

    async def get_portfolio(self):
        self.calls.append(("get_portfolio", ""))
        return self.portfolio


class FailingStopOrderManager(RecordingOrderManager):
    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
        self.calls.append(("place_stop_failed", round(stop_price, 2)))

        class Result:
            order_id = None
            error = "broker rejected stop"

        return Result()


class RepricingOrderManager(StubOrderManager):
    def __init__(
        self,
        *,
        fill_after_orders: int = 2,
        quote_bid: float = 2.70,
        quote_ask: float = 2.90,
        replacement_fill_price: float = 2.88,
        cancel_status_after_cancel: str = "CANCELED",
    ) -> None:
        self.fill_after_orders = fill_after_orders
        self.quote_bid = quote_bid
        self.quote_ask = quote_ask
        self.replacement_fill_price = replacement_fill_price
        self.cancel_status_after_cancel = cancel_status_after_cancel
        self.wait_calls = 0
        self.entry_calls: list[tuple[str, float]] = []
        self.cancel_calls: list[str] = []

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
        self.wait_calls += 1
        if self.wait_calls >= self.fill_after_orders:
            return True, {"status": "FILLED", "averageFillPrice": str(self.replacement_fill_price)}, None
        return False, {"status": "NEW"}, "fill_timeout"

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=self.quote_bid,
            ask=self.quote_ask,
            last=(self.quote_bid + self.quote_ask) / 2,
            open_interest=500,
            outcome="SUCCESS",
        )

    async def preflight_entry(self, option_symbol: str, limit_price: float, quantity: int):
        return PreflightCheck(
            payload={"limitPrice": f"{limit_price:.2f}"},
            current_increment=0.05,
            buying_power_requirement=limit_price * quantity * 100,
            estimated_cost=limit_price * quantity * 100,
        )

    async def cancel_order(self, order_id: str):
        self.cancel_calls.append(order_id)
        return True, None

    async def get_order_status(self, order_id: str):
        if order_id in self.cancel_calls:
            payload = {"status": self.cancel_status_after_cancel}
            if self.cancel_status_after_cancel == "FILLED":
                payload["averageFillPrice"] = str(self.replacement_fill_price)
            return self.cancel_status_after_cancel, payload, None
        return "NEW", {"status": "NEW"}, None

    async def place_entry_order(self, option_symbol: str, limit_price: float, quantity: int, *, order_id: str | None = None):
        self.entry_calls.append((order_id or "", limit_price))
        return OrderResult(order_id=order_id or "REPRICE123")


class StatusMapOrderManager(RecordingOrderManager):
    def __init__(self, statuses: dict[str, tuple[str | None, dict | None, str | None]]):
        super().__init__()
        self.statuses = statuses

    async def get_order_status(self, order_id: str):
        return self.statuses.get(order_id, (None, None, "missing_status"))


class ExplodingStatusOrderManager(RecordingOrderManager):
    async def get_order_status(self, order_id: str):
        raise AssertionError(f"paper order id should not be polled: {order_id}")


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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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
    assert [row[0] for row in rows] == ["entry_fill_check", "protective_stop_submission", "lifecycle_transition"]


def test_execution_supervisor_reprices_unfilled_entry_before_protection(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=2, replacement_fill_price=2.88)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[0],
            entry_reprice_cancel_after_seconds=1,
            entry_reprice_spread_pcts=[0.50],
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE_REPRICE",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.order_id != "ENTRY123"
    assert protected.estimated_entry_price == 2.88
    assert protected.risk_details["broker_average_fill_price"] == 2.88
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert order_manager.entry_calls[0][1] == 2.90
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "entry_order_repriced" in event_types
    assert "protective_stop_submission" in event_types


def test_execution_supervisor_protects_entry_if_cancel_race_fills_old_order(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(
        fill_after_orders=99,
        replacement_fill_price=2.87,
        cancel_status_after_cancel="FILLED",
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[0],
            entry_reprice_cancel_after_seconds=1,
            entry_reprice_spread_pcts=[0.50],
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE_CANCEL_RACE",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.order_id == "ENTRY123"
    assert protected.estimated_entry_price == 2.87
    assert protected.stop_order_id == "STOP123"
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "entry_reprice_cancel_race_filled" in event_types
    assert "protective_stop_submission" in event_types


def test_execution_supervisor_reprices_when_cancel_status_receipt_is_delayed(tmp_path) -> None:
    class SlowCancelReceiptOrderManager(RepricingOrderManager):
        async def get_order_status(self, order_id: str):
            await asyncio.sleep(2)
            return await super().get_order_status(order_id)

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = SlowCancelReceiptOrderManager(fill_after_orders=2, replacement_fill_price=2.88)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[0],
            entry_reprice_cancel_after_seconds=1,
            entry_reprice_spread_pcts=[0.50],
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE_CANCEL_RECEIPT_DELAY",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.order_id != "ENTRY123"
    assert protected.stop_order_id == "STOP123"
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert len(order_manager.entry_calls) == 1
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = next(
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM events WHERE event_type = 'entry_order_repriced'")
        )
    assert payload["replacement_order_id"] == protected.order_id


def test_execution_supervisor_cancels_unfilled_entry_after_reprice_ceiling(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[],
            entry_reprice_cancel_after_seconds=0,
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_CANCEL",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="live_pending",
        order_id="ENTRY123",
    )
    plan = TradePlan(
        trade_id="TRADE_CANCEL",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert order_manager.cancel_calls == ["ENTRY123"]
    assert supervisor.planner.position_tracker.active_positions() == []
    assert supervisor.lifecycle_store.get("QQQ", deployment.deployment_id).state.value == "closed"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "entry_reprice_cancel_after_timeout" in event_types


def test_execution_supervisor_cancels_when_reprice_quote_is_too_wide(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=2.00, quote_ask=2.90)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[0],
            entry_reprice_cancel_after_seconds=1,
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_WIDE",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        source="live_pending",
        order_id="ENTRY123",
    )
    plan = TradePlan(
        trade_id="TRADE_WIDE",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert order_manager.cancel_calls == ["ENTRY123"]
    assert order_manager.entry_calls == []
    assert supervisor.planner.position_tracker.active_positions() == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    blocked_payload = next(json.loads(row[1]) for row in rows if row[0] == "entry_reprice_blocked")
    assert "public_spread_above_maximum" in blocked_payload["reason"]


def test_execution_supervisor_records_live_entry_unprotected_when_initial_stop_fails(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "trades.db"))
    bus = InMemoryEventBus()
    queue = bus.subscribe(TradeLifecycleTransitionEvent)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(FailingStopOrderManager()),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        event_bus=bus,
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE_STOP_FAIL",
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
        protected = await supervisor._protect_live_entry(plan, deployment)
        event = await queue.get()
        return protected, event

    protected, event = asyncio.run(run())

    assert protected.stop_order_id is None
    assert protected.risk_details["protection_error"] == "broker rejected stop"
    assert supervisor.planner.position_tracker.active_positions()[0].stop_order_id is None
    assert event.new_state == "open_unprotected"
    trades = asyncio.run(trade_repo.get_open_trades())
    assert trades[0].status == "open_unprotected"
    assert trades[0].stop_order_id is None
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
        runtime_issue = conn.execute("SELECT payload FROM events WHERE event_type = 'runtime_issue'").fetchone()[0]
    assert event_types == ["entry_fill_check", "protective_stop_submission", "runtime_issue", "lifecycle_transition"]
    issue_payload = json.loads(runtime_issue)
    assert issue_payload["category"] == "protective_stop_failure"
    assert issue_payload["error"] == "broker rejected stop"


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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    base_deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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


def test_execution_supervisor_tracks_shadow_plan_as_paper_position(tmp_path) -> None:
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 558.0},
    )

    class ShadowPlanner(StubPlanner):
        async def plan_entry(self, *args, **kwargs):
            del args, kwargs
            return TradePlan(
                trade_id="SHADOW1",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                direction=SignalDirection.SHORT,
                option_symbol="QQQ260330P00558000",
                quantity=1,
                estimated_entry_price=2.0,
                risk_reasons=["approved"],
                dry_run=True,
                order_id=None,
                underlying_entry_price=558.0,
                entry_timestamp=decision.timestamp,
            )

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "trades.db"))
    supervisor = ExecutionSupervisor(
        planner=ShadowPlanner(),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    positions = supervisor.planner.position_tracker.active_positions()
    assert len(positions) == 1
    assert positions[0].source == "shadow"
    assert positions[0].order_id == "SHADOW_ENTRY"
    trades = asyncio.run(trade_repo.get_open_trades())
    assert len(trades) == 1
    assert trades[0].status == "open_unprotected"
    assert trades[0].underlying_entry_price == 558.0
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
        shadow_payload = conn.execute(
            "SELECT payload FROM events WHERE event_type = 'shadow_entry_assumed'"
        ).fetchone()[0]
    assert "shadow_entry_assumed" in event_types
    assert json.loads(shadow_payload)["underlying_entry_price"] == 558.0


def test_execution_supervisor_does_not_open_shadow_position_when_risk_rejects(tmp_path) -> None:
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 558.0},
    )

    class RejectedShadowPlanner(StubPlanner):
        async def plan_entry(self, *args, **kwargs):
            del args, kwargs
            return TradePlan(
                trade_id="SHADOW_REJECTED",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                direction=SignalDirection.SHORT,
                option_symbol="QQQ260330P00558000",
                quantity=1,
                estimated_entry_price=2.0,
                risk_reasons=["max_open_positions_per_symbol_reached"],
                dry_run=True,
                order_id=None,
                underlying_entry_price=558.0,
                entry_timestamp=decision.timestamp,
            )

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "trades.db"))
    supervisor = ExecutionSupervisor(
        planner=RejectedShadowPlanner(),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    plan = asyncio.run(supervisor.handle_signal(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    assert plan.risk_reasons == ["max_open_positions_per_symbol_reached"]
    assert supervisor.planner.position_tracker.active_positions() == []
    assert asyncio.run(trade_repo.get_open_trades()) == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "trade_plan" in event_types
    assert "shadow_entry_assumed" not in event_types


def test_execution_supervisor_records_shadow_exit_pnl(tmp_path) -> None:
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "trades.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(RecordingOrderManager(quote_bid=3.0)),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="SHADOW1",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.0,
        source="shadow",
        order_id="SHADOW_ENTRY",
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
    )
    tracked_position = supervisor.planner.position_tracker.active_positions()[0]
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="SHADOW1",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260330P00558000",
                quantity=1,
                entry_price=2.0,
                status="open_unprotected",
                entry_order_id="SHADOW_ENTRY",
            )
        )
    )
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 19, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["test_exit"],
        features={},
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, tracked_position, decision, dry_run=True))

    assert plan is not None
    recent = asyncio.run(trade_repo.get_recent_trades(limit=5))
    assert recent[0].status == "closed"
    assert recent[0].exit_price == 3.0
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    assert "shadow_exit_assumed" in [row[0] for row in rows]
    shadow_exit_payload = next(json.loads(row[1]) for row in rows if row[0] == "shadow_exit_assumed")
    assert shadow_exit_payload["realized_pnl_usd"] == 100.0


def test_execution_supervisor_forces_shadow_management_to_dry_run(tmp_path) -> None:
    from bhiksha.config.loader import load_deployments

    order_manager = RecordingOrderManager(quote_bid=3.0)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="SHADOW1",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.0,
        source="shadow",
        order_id="SHADOW_ENTRY",
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.stop_order_id == "DRY_RUN_STOP"
    assert ("place_stop", 1.3) not in order_manager.calls
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    assert "shadow_mark" in [row[0] for row in rows]
    stop_payload = next(json.loads(row[1]) for row in rows if row[0] == "protective_stop_submission")
    assert stop_payload["dry_run"] is True
    assert stop_payload["source"] == "shadow"
    assert stop_payload["stop_order_id"] == "DRY_RUN_STOP"


def test_execution_supervisor_closes_shadow_trade_when_option_stop_is_breached(tmp_path) -> None:
    order_manager = RecordingOrderManager(quote_bid=1.0)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "trades.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="SHADOW_STOP",
        option_symbol="QQQ260522P00703000",
        quantity=1,
        entry_price=2.0,
        source="shadow",
        order_id="SHADOW_ENTRY",
        stop_order_id="DRY_RUN_STOP",
        stop_price=1.3,
        entry_timestamp=datetime(2026, 5, 20, 13, 38, tzinfo=UTC),
    )
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="SHADOW_STOP",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260522P00703000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 5, 20, 13, 38, tzinfo=UTC),
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
                stop_order_id="DRY_RUN_STOP",
                stop_price=1.3,
            )
        )
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is None
    assert supervisor.planner.position_tracker.active_positions() == []
    recent = asyncio.run(trade_repo.get_recent_trades(limit=1))
    assert recent[0].status == "closed"
    assert recent[0].exit_price == 1.0
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    event_types = [row[0] for row in rows]
    assert "shadow_mark" in event_types
    assert "exit_decision" in event_types
    assert "shadow_exit_assumed" in event_types
    shadow_exit_payload = next(json.loads(row[1]) for row in rows if row[0] == "shadow_exit_assumed")
    assert shadow_exit_payload["reason"] == ["shadow_option_stop_loss"]
    assert shadow_exit_payload["exit_price"] == 1.0
    assert shadow_exit_payload["realized_stop_r"] <= -1.0


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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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


def test_execution_supervisor_holds_fill_timeout_for_reconciliation(tmp_path) -> None:
    class TimeoutOrderManager(StubOrderManager):
        async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
            del order_id, timeout_seconds, poll_seconds
            return False, {"status": "NEW"}, "fill_timeout"

    class TimeoutPlanner(StubPlanner):
        def __init__(self):
            super().__init__()
            self.order_manager = TimeoutOrderManager()
            self.cash_guard = RecordingCashGuard()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    reconcile_trigger = asyncio.Event()
    supervisor = ExecutionSupervisor(
        planner=TimeoutPlanner(),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
        reconcile_trigger=reconcile_trigger,
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
    )

    async def run() -> None:
        await trade_repo.upsert_trade(
            TradeRecord(
                trade_id=plan.trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                option_symbol=plan.option_symbol,
                quantity=plan.quantity,
                entry_price=plan.estimated_entry_price,
                entry_timestamp=plan.entry_timestamp,
                status="pending_entry",
                entry_order_id=plan.order_id,
            )
        )
        await supervisor._protect_live_entry(plan, deployment)

    asyncio.run(run())

    open_trades = asyncio.run(trade_repo.get_open_trades())
    assert open_trades[0].status == "pending_entry_reconcile"
    assert supervisor.lifecycle_store.get("QQQ", deployment.deployment_id).state.value == "reconciliation_hold"
    assert reconcile_trigger.is_set() is True
    assert supervisor.planner.cash_guard.calls == []


def test_execution_supervisor_sync_lifecycle_recovers_timed_out_entry(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    planner = StubPlanner()
    planner.cash_guard = RecordingCashGuard()
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    async def run() -> None:
        await trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE123",
                deployment_id="market_impulse_qqq_short_v1",
                symbol="QQQ",
                option_symbol="QQQ260330P00558000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                status="pending_entry_reconcile",
                entry_order_id="ENTRY123",
            )
        )
        supervisor.planner.position_tracker.open_position(
            "QQQ",
            "market_impulse_qqq_short_v1",
            trade_id="TRADE123",
            option_symbol="QQQ260330P00558000",
            quantity=1,
            entry_price=2.0,
            entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
            source="broker_sync",
            order_id="ENTRY123",
            stop_order_id="STOP123",
            stop_price=1.1,
        )
        await supervisor.sync_lifecycle()

    asyncio.run(run())

    open_trades = asyncio.run(trade_repo.get_open_trades())
    assert open_trades[0].status == "open_protected"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "entry_reconcile_recovered" in event_types


def test_execution_supervisor_sync_lifecycle_releases_terminal_reconcile_hold(tmp_path) -> None:
    class CanceledOrderManager(StubOrderManager):
        async def get_order_status(self, order_id: str):
            del order_id
            return "CANCELED", {"status": "CANCELED"}, None

    class ReconcilePlanner(StubPlanner):
        def __init__(self):
            super().__init__()
            self.order_manager = CanceledOrderManager()
            self.cash_guard = RecordingCashGuard()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=ReconcilePlanner(),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    async def run() -> None:
        await trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE123",
                deployment_id="market_impulse_qqq_short_v1",
                symbol="QQQ",
                option_symbol="QQQ260330P00558000",
                quantity=1,
                entry_price=2.0,
                entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                status="pending_entry_reconcile",
                entry_order_id="ENTRY123",
            )
        )
        await supervisor.sync_lifecycle()

    asyncio.run(run())

    assert asyncio.run(trade_repo.get_open_trades()) == []
    assert supervisor.planner.cash_guard.calls == [("release", "TRADE123")]
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "entry_reconcile_released" in event_types


def test_execution_supervisor_recovers_open_paper_trade_after_restart(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(ExplodingStatusOrderManager()),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    entry_at = datetime(2026, 3, 30, 14, 30, tzinfo=UTC)

    async def run() -> None:
        await trade_repo.upsert_trade(
            TradeRecord(
                trade_id="SHADOW123",
                deployment_id="market_impulse_qqq_short_v1",
                symbol="QQQ",
                option_symbol="QQQ260330P00558000",
                quantity=1,
                entry_price=2.0,
                underlying_entry_price=558.0,
                entry_timestamp=entry_at,
                status="open_protected",
                entry_order_id="SHADOW_ENTRY",
                stop_order_id="DRY_RUN_STOP",
                stop_price=1.3,
            )
        )
        await supervisor.sync_lifecycle()

    asyncio.run(run())

    open_trades = asyncio.run(trade_repo.get_open_trades())
    positions = supervisor.planner.position_tracker.active_positions()
    assert len(open_trades) == 1
    assert open_trades[0].status == "open_protected"
    assert len(positions) == 1
    assert positions[0].source == "shadow"
    assert positions[0].order_id == "SHADOW_ENTRY"
    assert positions[0].stop_order_id == "DRY_RUN_STOP"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    assert "paper_position_recovered" in [row[0] for row in rows]
    recovered = next(json.loads(row[1]) for row in rows if row[0] == "paper_position_recovered")
    assert recovered["trade_id"] == "SHADOW123"
    assert recovered["source"] == "shadow"


def test_execution_supervisor_sanitizes_recovered_stop_below_bid(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RecordingOrderManager(quote_bid=1.30)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    base_deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = base_deployment.model_copy(
        update={
            "exit": base_deployment.exit.model_copy(
                update={
                    "stop_loss_pct": 0.35,
                    "use_profit_target": False,
                    "profit_target_multiple": None,
                }
            )
        }
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE123",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        source="broker_sync",
        order_id="ENTRY123",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    updated = asyncio.run(supervisor._restore_missing_protection(deployment, position, dry_run=False))

    assert ("place_stop", 1.29) in order_manager.calls
    assert updated.stop_order_id == "STOP123"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = conn.execute(
            "SELECT payload FROM events WHERE event_type = 'protective_stop_submission' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert '"stop_sanitized": true' in payload
    assert '"quote_bid": 1.3' in payload


def test_execution_supervisor_skips_restore_when_active_close_order_exists(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RecordingOrderManager(
        portfolio={
            "orders": [
                {
                    "orderId": "STOP_EXISTING",
                    "instrument": {"symbol": "QQQ260330P00558000", "type": "OPTION"},
                    "type": "STOP",
                    "side": "SELL",
                    "status": "NEW",
                    "openCloseIndicator": "CLOSE",
                    "stopPrice": "1.30",
                }
            ]
        }
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE123",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        source="broker_sync",
        order_id="ENTRY123",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    updated = asyncio.run(supervisor._restore_missing_protection(deployment, position, dry_run=False))

    assert ("place_stop", 1.1) not in order_manager.calls
    assert updated.stop_order_id == "STOP_EXISTING"
    assert updated.stop_price == 1.3
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert "protection_restore_skipped" in event_types
    assert "protective_stop_submission" not in event_types


def test_execution_supervisor_hard_flats_due_positions(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
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
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = StatusMapOrderManager(
        {
            "CLOSE123": (
                "FILLED",
                {
                    "orderId": "CLOSE123",
                    "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "2.35",
                    "closedAt": "2026-03-30T14:36:12Z",
                },
                None,
            )
        }
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE123",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                quantity=1,
                entry_price=2.5,
                entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY123",
            )
        )
    )
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
    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at,
                   exit_order_status, exit_order_type
            FROM trade_sessions
            WHERE trade_id = 'TRADE123'
            """
        ).fetchone()
    assert row == (
        "closed",
        "CLOSE123",
        2.35,
        1,
        "2026-03-30T14:36:12+00:00",
        "FILLED",
        "LIMIT",
    )


def test_execution_supervisor_enriches_stop_filled_disappeared_position(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = StatusMapOrderManager(
        {
            "STOP123": (
                "FILLED",
                {
                    "orderId": "STOP123",
                    "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "STOP",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "1.20",
                    "closedAt": "2026-03-30T15:01:00Z",
                    "stopPrice": "1.20",
                },
                None,
            )
        }
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE123",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                quantity=1,
                entry_price=2.5,
                entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY123",
                stop_order_id="STOP123",
                stop_price=1.2,
            )
        )
    )

    asyncio.run(supervisor.sync_lifecycle())

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at,
                   exit_order_status, exit_order_type
            FROM trade_sessions
            WHERE trade_id = 'TRADE123'
            """
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]

    assert row == (
        "closed",
        "STOP123",
        1.2,
        1,
        "2026-03-30T15:01:00+00:00",
        "FILLED",
        "STOP",
    )
    assert "exit_fill_enriched" in event_types


def test_execution_supervisor_arms_virtual_profit_target_when_broker_supports_single_exit_order(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=StubPlanner(),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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
        "protective_stop_submission",
        "lifecycle_transition",
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

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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


def test_execution_supervisor_uses_option_profit_target_pct(tmp_path) -> None:
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

    base = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={
            "exit": base.exit.model_copy(
                update={
                    "use_profit_target": True,
                    "profit_target_multiple": None,
                    "option_profit_target_pct": 0.35,
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

    assert protected.target_order_id == "TARGET123"
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.target_price == 2.7


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

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    event_types = [row[0] for row in rows]
    assert "target_approach_detected" in event_types
    approach_payload = json.loads(next(row[1] for row in rows if row[0] == "target_approach_detected"))
    assert approach_payload["target_approach_offset_pct"] == 0.02
    assert approach_payload["activation_price"] == 3.35 * 0.98


def test_execution_supervisor_virtual_target_activation_allows_ambiguous_cancel(tmp_path) -> None:
    order_manager = RecordingOrderManager(quote_bid=3.30, cancel_success=False)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    from bhiksha.config.loader import load_deployments

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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

    base = _enabled_deployment("market_impulse_qqq_short_v1")
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
        target_activation_price=3.25,
        target_activation_high_price=3.25,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    managed = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert managed is not None
    assert managed.target_order_id is None
    assert managed.stop_order_id == "STOP123"
    assert managed.stop_price == 1.1
    assert ("cancel", "TARGET123") in order_manager.calls
    assert ("place_stop", 1.1) in order_manager.calls


# =========================================================================== #
# Profile-exit hardening: C1 partial-close, H2 breakeven stop, H3 state persist
# =========================================================================== #

from bhiksha.execution.profile_exit import (  # noqa: E402
    ProfileExitFields,
    ProfileExitState,
    ProfileFsmAction,
    ProfileLadderRule,
    ProfileMarketView,
    evaluate_profile_exit,
)


class _QtyRecordingOrderManager(StubOrderManager):
    """Order manager that records the QUANTITY passed to close/stop placements."""

    def __init__(self) -> None:
        self.close_calls: list[tuple[str, int, str]] = []
        self.stop_calls: list[tuple[str, float, int]] = []
        self.cancel_calls: list[str] = []

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        self.close_calls.append((option_symbol, int(quantity), exit_mode.value))
        return OrderResult(order_id="CLOSE_PARTIAL")

    async def place_stop_loss_order(self, option_symbol, stop_price, quantity, *, order_id=None):
        self.stop_calls.append((option_symbol, round(stop_price, 2), int(quantity)))
        return OrderResult(order_id="STOP_RESIDUAL")

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return True, None

    async def get_option_quote(self, option_symbol):
        return PublicQuote(symbol=option_symbol, bid=2.0, ask=2.05, last=2.02, open_interest=500, outcome="SUCCESS")


def _partial_supervisor(tmp_path):
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    om = _QtyRecordingOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    return supervisor, om


def _partial_scale_decision(deployment_id, *, exit_quantity, symbol="QQQ"):
    return ExitDecision(
        deployment_id=deployment_id,
        symbol=symbol,
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_target_1_partial"],
        cancel_protection_orders=True,
        features={"exit_quantity": exit_quantity, "partial_scale": True, "profile_id": "FLASH_REVERSAL"},
    )


def test_partial_scale_closes_only_banked_qty_and_keeps_residual_live(tmp_path) -> None:
    # C1: live partial -> close 3 of 4, keep 1 open with a re-armed stop.
    supervisor, om = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_FULL",
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = _partial_scale_decision(deployment.deployment_id, exit_quantity=3)

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    # Only 3 contracts were closed.
    assert om.close_calls == [("QQQ260401P00556000", 3, ExitMode.STRATEGY.value)]
    assert plan is not None and plan.quantity == 3
    # The residual (1) remains OPEN, not flattened, with a re-armed stop on 1 qty.
    survivors = supervisor.planner.position_tracker.active_positions()
    assert len(survivors) == 1
    residual = survivors[0]
    assert residual.quantity == 1
    assert residual.exit_mode is None and residual.exit_order_id is None
    assert residual.stop_order_id == "STOP_RESIDUAL"
    assert om.stop_calls == [("QQQ260401P00556000", 1.5, 1)]


def test_partial_scale_dry_run_keeps_residual_and_places_no_orders(tmp_path) -> None:
    supervisor, om = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=2.0,
        source="shadow",
        stop_order_id="STOP_FULL",
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = _partial_scale_decision(deployment.deployment_id, exit_quantity=3)

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=True))

    assert plan is not None and plan.dry_run is True and plan.quantity == 3
    assert om.close_calls == []  # no broker order in dry-run
    residual = supervisor.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1
    assert residual.exit_mode is None


def test_partial_scale_can_never_close_full_position(tmp_path) -> None:
    # Hard guard: a partial_scale carrying full (or larger) qty must NOT flatten.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_FULL",
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    bad = _partial_scale_decision(deployment.deployment_id, exit_quantity=4)  # == full qty

    import pytest

    with pytest.raises(ValueError, match="must never close the full position"):
        asyncio.run(supervisor.handle_exit(deployment, position, bad, dry_run=False))
    # Position untouched (still 4, still open).
    survivor = supervisor.planner.position_tracker.active_positions()[0]
    assert survivor.quantity == 4 and survivor.exit_mode is None


def test_breakeven_stop_to_breakeven_moves_protective_stop(tmp_path) -> None:
    # H2: a STOP_TO_BREAKEVEN decision (action="hold", replacement_stop_price set)
    # must cancel/replace the live stop at the new price.
    supervisor, om = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_OLD",
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        exit=False,
        action="hold",
        reason=["profile_stop_to_breakeven"],
        replacement_stop_price=2.0,  # entry premium
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is None  # hold-class decision returns no ExitPlan
    assert "STOP_OLD" in om.cancel_calls  # old stop canceled
    assert om.stop_calls == [("QQQ260401P00556000", 2.0, 1)]  # new stop at breakeven
    moved = supervisor.planner.position_tracker.active_positions()[0]
    assert moved.stop_order_id == "STOP_RESIDUAL"
    assert moved.stop_price == 2.0
    assert moved.exit_mode is None  # still open, just better protected


def test_breakeven_stop_noop_when_already_at_price(tmp_path) -> None:
    supervisor, om = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_BE",
        stop_price=2.0,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        exit=False,
        action="hold",
        reason=["profile_stop_to_breakeven"],
        replacement_stop_price=2.0,
    )
    asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert om.stop_calls == []  # already at breakeven -> no churn
    assert om.cancel_calls == []


# --- H3: ProfileExitState persists across monitor ticks (supervisor-owned) ---


def _flash_fields():
    return ProfileExitFields(
        profile_id="FLASH_REVERSAL",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.75,
        initial_stop_pct=0.25,
        premium_disaster_stop_pct=0.30,
        high_water_giveback_policy="STRICT",
        breakeven_after_t1=True,
        eod_flat=False,
    )


def _tick(supervisor, position, fields, premium, *, entry_premium, now):
    state = supervisor.get_or_create_profile_exit_state(position, entry_premium=entry_premium)
    return evaluate_profile_exit(
        fields=fields,
        entry_premium=entry_premium,
        quantity=position.quantity,
        market=ProfileMarketView(current_premium=premium, bar_time_et=None),
        entry_time=position.entry_timestamp,
        now=now,
        state=state,
    )


def test_profile_state_persists_across_ticks_no_rebanked_partial(tmp_path) -> None:
    # H3: the same persisted state is reused tick-to-tick, so T1 banks ONCE,
    # breakeven is emitted ONCE, and the giveback high-water mark is not reset.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    entry_ts = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=1.0,
        entry_timestamp=entry_ts,
        source="live_open",
        stop_order_id="STOP_FULL",
        stop_price=0.75,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]

    # Tick 1: premium >= T1 (1.25) -> partial bank fires once.
    d1 = _tick(supervisor, position, _flash_fields(), 1.26, entry_premium=1.0, now=entry_ts.replace(minute=1))
    assert d1.rule is ProfileLadderRule.TARGET_1_PARTIAL
    assert d1.fsm_action is ProfileFsmAction.PARTIAL_SCALE
    assert d1.exit_quantity == 3

    # Tick 2: still elevated -> STOP_TO_BREAKEVEN emitted ONCE (not a re-bank).
    d2 = _tick(supervisor, position, _flash_fields(), 1.24, entry_premium=1.0, now=entry_ts.replace(minute=2))
    assert d2.fsm_action is ProfileFsmAction.STOP_TO_BREAKEVEN

    # Tick 3: still elevated -> NOT a partial again, NOT another breakeven emit.
    d3 = _tick(supervisor, position, _flash_fields(), 1.23, entry_premium=1.0, now=entry_ts.replace(minute=3))
    assert d3.rule is not ProfileLadderRule.TARGET_1_PARTIAL
    assert d3.fsm_action is not ProfileFsmAction.STOP_TO_BREAKEVEN

    # The persisted state proves the partial was banked exactly once.
    state = supervisor.get_or_create_profile_exit_state(position, entry_premium=1.0)
    assert state.target_1_banked is True
    assert state.banked_quantity == 3
    assert state.breakeven_emitted is True


def test_profile_state_high_water_not_reset_across_ticks(tmp_path) -> None:
    # Without persistence the giveback high-water mark would reset each tick and
    # never arm. With persistence, peak_premium carries forward and giveback fires.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    entry_ts = datetime(2026, 3, 30, 14, 0, tzinfo=UTC)
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=1.0,
        entry_timestamp=entry_ts,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    fields = ProfileExitFields(
        profile_id="G",
        initial_stop_pct=0.25,
        target_1_r=None,
        target_2_r=10.0,
        high_water_giveback_policy="STRICT",
        eod_flat=False,
    )
    # Tick 1 sets peak to 1.30 (r=1.2). Tick 2 gives back to 1.15 -> fires.
    _tick(supervisor, position, fields, 1.30, entry_premium=1.0, now=entry_ts.replace(minute=1))
    state = supervisor.get_or_create_profile_exit_state(position, entry_premium=1.0)
    assert state.peak_premium == 1.30  # high-water persisted
    d2 = _tick(supervisor, position, fields, 1.15, entry_premium=1.0, now=entry_ts.replace(minute=2))
    assert d2.rule is ProfileLadderRule.HIGH_WATER_GIVEBACK


def test_profile_state_cleared_on_close(tmp_path) -> None:
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=1.0,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    state = supervisor.get_or_create_profile_exit_state(position, entry_premium=1.0)
    state.peak_premium = 1.50
    key = supervisor._profile_state_key(position)
    assert key in supervisor._profile_exit_states
    supervisor.clear_profile_exit_state(position)
    assert key not in supervisor._profile_exit_states
    # A fresh get after clear starts clean (peak == entry again).
    fresh = supervisor.get_or_create_profile_exit_state(position, entry_premium=1.0)
    assert fresh.peak_premium == 1.0
