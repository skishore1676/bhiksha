import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from bhiksha.config.models import AppConfig
from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import ExitMode, SignalDirection
from bhiksha.domain.models import ExitDecision, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.order_manager import OrderResult, PreflightCheck, PublicQuote
from bhiksha.execution.supervisor import (
    ExecutionSupervisor,
    _entry_reprice_cancel_after_seconds,
    _entry_reprice_checkpoints,
    _entry_reprice_enabled,
    _entry_reprice_spread_fraction,
)
from bhiksha.persistence.sqlite import SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from bhiksha.state.position_tracker import LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE, PositionTracker
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
        cancel_filled_quantity: int | str | None = None,
        cancel_order_quantity: int | None = None,
    ) -> None:
        self.fill_after_orders = fill_after_orders
        self.quote_bid = quote_bid
        self.quote_ask = quote_ask
        self.replacement_fill_price = replacement_fill_price
        self.cancel_status_after_cancel = cancel_status_after_cancel
        self.cancel_filled_quantity = cancel_filled_quantity
        self.cancel_order_quantity = cancel_order_quantity
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
            if self.cancel_filled_quantity is not None:
                payload["filledQuantity"] = self.cancel_filled_quantity
            if self.cancel_order_quantity is not None:
                payload["quantity"] = self.cancel_order_quantity
            if self.cancel_status_after_cancel == "FILLED":
                payload["averageFillPrice"] = str(self.replacement_fill_price)
            elif self.cancel_filled_quantity:
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


def test_execution_supervisor_tags_can_ladder_at_live_entry_and_survives_partial_residual(tmp_path) -> None:
    """ITEM D (2026-07-08 hygiene batch): can_ladder = quantity >= 2 is tagged
    at live entry recording time and, unlike quantity itself, must NOT be
    clobbered when a later partial bank overwrites trade_sessions.quantity to
    the residual (see the QQQ fixture in
    test_partial_leg_pnl_fixture_qqq_t1_bank_then_runner_exit for that
    overwrite happening).
    """
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(StubOrderManager()),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")

    # 1-lot live entry -> no-ladder.
    one_lot_plan = TradePlan(
        trade_id="TRADE_1LOT",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_1LOT",
    )
    asyncio.run(supervisor._protect_live_entry(one_lot_plan, deployment))

    # 2-lot live entry -> ladder-capable.
    two_lot_plan = TradePlan(
        trade_id="TRADE_2LOT",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00559000",
        quantity=2,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_2LOT",
    )
    asyncio.run(supervisor._protect_live_entry(two_lot_plan, deployment))

    open_trades = {trade.trade_id: trade for trade in asyncio.run(trade_repo.get_open_trades())}
    assert open_trades["TRADE_1LOT"].can_ladder is False
    assert open_trades["TRADE_2LOT"].can_ladder is True

    # A later upsert reflecting a reduced (partial-residual) quantity, WITHOUT
    # re-stating can_ladder, must not erase the entry-time tag (COALESCE, same
    # precedent as exit_rule).
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_2LOT",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260330P00559000",
                quantity=1,
                status="open_protected",
            )
        )
    )
    survivor = asyncio.run(trade_repo.get_open_trades())
    residual = next(trade for trade in survivor if trade.trade_id == "TRADE_2LOT")
    assert residual.quantity == 1  # the residual, not the original 2
    assert residual.can_ladder is True  # the entry-time tag survives


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


def test_execution_supervisor_uses_lane_patient_reprice_policy_when_global_policy_is_off(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=2, replacement_fill_price=2.73)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            order_fill_timeout_seconds=1,
            entry_reprice_enabled=False,
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "entry_reprice_enabled": True,
                    "entry_reprice_checkpoints_seconds": [0],
                    "entry_reprice_cancel_after_seconds": 1,
                    "entry_reprice_spread_fractions": [0.25],
                    "entry_pricing_oi_percentile_scale": True,
                }
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_PATIENT_REPRICE",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.70,
        risk_reasons=["approved"],
        risk_details={
            "open_interest_percentile": 0.50,
            "entry_pricing": {
                "initial_profile_comparison": {
                    "patient": {"quote_limit_price": 2.71},
                    "balanced": {"quote_limit_price": 2.73},
                    "urgent": {"quote_limit_price": 2.75},
                }
            },
        },
        dry_run=False,
        order_id="ENTRY123",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.order_id != "ENTRY123"
    assert order_manager.entry_calls[0][1] == 2.73
    assert protected.risk_details["entry_pricing"]["policy"]["spread_fraction"] == 0.125
    assert protected.risk_details["entry_pricing"]["initial_profile_comparison"] == {
        "patient": {"quote_limit_price": 2.71},
        "balanced": {"quote_limit_price": 2.73},
        "urgent": {"quote_limit_price": 2.75},
    }


def test_named_patient_profile_resolves_full_lane_policy_when_global_policy_is_off() -> None:
    app_config = AppConfig(entry_reprice_enabled=False)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "entry_execution_profile": "patient",
                    "entry_reprice_enabled": None,
                    "entry_reprice_checkpoints_seconds": None,
                    "entry_reprice_cancel_after_seconds": None,
                    "entry_reprice_spread_fractions": None,
                }
            )
        }
    )

    assert _entry_reprice_enabled(app_config, deployment) is True
    assert _entry_reprice_checkpoints(app_config, deployment) == [60, 180]
    assert _entry_reprice_cancel_after_seconds(app_config, deployment) == 300
    assert _entry_reprice_spread_fraction(deployment, 1) == 0.50
    assert _entry_reprice_spread_fraction(deployment, 2) == 0.70

    disabled = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(update={"entry_reprice_enabled": False})
        }
    )
    assert _entry_reprice_enabled(app_config, disabled) is False


def test_reprice_above_profile_chase_cap_leaves_existing_order_resting(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=3.00, quote_ask=3.20)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(entry_reprice_enabled=False),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={"entry_execution_profile": "patient"}
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_CHASE_REST",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.70,
        risk_reasons=["approved"],
        risk_details={"entry_pricing": {"initial_limit_price": 2.70}},
        dry_run=False,
        order_id="ENTRY123",
    )

    result = asyncio.run(supervisor._reprice_live_entry(plan, deployment, attempt=1))

    assert result.error is None
    assert result.plan.order_id == "ENTRY123"
    assert order_manager.cancel_calls == []
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE event_type = 'entry_reprice_chase_guard_resting'"
            ).fetchone()[0]
        )
    assert payload["decision"] == "rest_existing_order"
    assert payload["reference_limit_price"] == 2.70
    assert payload["proposed_limit_price"] == 3.00
    assert payload["max_chase_pct"] == 0.10
    assert payload["max_chase_price"] == 2.97


def test_reprice_chase_cap_remains_anchored_to_original_limit(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=2.85, quote_ask=2.95)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(entry_reprice_enabled=False),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={"entry_execution_profile": "patient"}
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_CHASE_ANCHOR",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.70,
        risk_reasons=["approved"],
        risk_details={"entry_pricing": {"initial_limit_price": 2.70}},
        dry_run=False,
        order_id="ENTRY123",
    )

    first = asyncio.run(supervisor._reprice_live_entry(plan, deployment, attempt=1))
    order_manager.quote_bid = 3.00
    order_manager.quote_ask = 3.10
    second = asyncio.run(supervisor._reprice_live_entry(first.plan, deployment, attempt=2))

    assert first.plan.estimated_entry_price == 2.85
    assert first.plan.risk_details["entry_pricing"]["initial_limit_price"] == 2.70
    assert second.plan.order_id == first.plan.order_id
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert len(order_manager.entry_calls) == 1


def test_reprice_chase_cap_does_not_block_a_lower_replacement(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=2.90, quote_ask=3.00)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(entry_reprice_enabled=False),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={"entry_execution_profile": "patient"}
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_CHASE_LOWER",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=3.00,
        risk_reasons=["approved"],
        risk_details={"entry_pricing": {"initial_limit_price": 2.70}},
        dry_run=False,
        order_id="ENTRY_HIGH",
    )

    result = asyncio.run(supervisor._reprice_live_entry(plan, deployment, attempt=1))

    assert result.plan.estimated_entry_price == 2.90
    assert order_manager.cancel_calls == ["ENTRY_HIGH"]
    assert order_manager.entry_calls[0][1] == 2.90


def test_order_resting_above_chase_cap_is_cancelled_at_profile_deadline(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=3.00, quote_ask=3.20)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, entry_reprice_enabled=False),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "entry_execution_profile": "patient",
                    "entry_reprice_checkpoints_seconds": [0],
                    "entry_reprice_spread_fractions": [0.50],
                    "entry_reprice_cancel_after_seconds": 1,
                }
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_CHASE_TIMEOUT",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.70,
        risk_reasons=["approved"],
        risk_details={"entry_pricing": {"initial_limit_price": 2.70}},
        dry_run=False,
        order_id="ENTRY_RESTING",
    )

    result = asyncio.run(supervisor._wait_for_entry_fill_or_cancel(plan, deployment))

    assert result.cancelled_without_fill is True
    assert order_manager.cancel_calls == ["ENTRY_RESTING"]
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reprice_chase_guard_resting" in event_types
    assert "entry_reprice_cancel_after_timeout" in event_types


def test_execution_supervisor_lane_cancel_deadline_removes_unfilled_order(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, entry_reprice_enabled=False),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "entry_reprice_enabled": True,
                    "entry_reprice_checkpoints_seconds": [],
                    "entry_reprice_cancel_after_seconds": 0,
                    "entry_reprice_spread_fractions": [],
                }
            )
        }
    )
    plan = TradePlan(
        trade_id="TRADE_PATIENT_CANCEL",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.70,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    result = asyncio.run(supervisor._wait_for_entry_fill_or_cancel(plan, deployment))

    assert result.cancelled_without_fill is True
    assert order_manager.cancel_calls == ["ENTRY123"]
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE event_type = 'entry_reprice_cancel_after_timeout'"
            ).fetchone()[0]
        )
    assert payload["cancel_after_seconds"] == 0


def test_execution_supervisor_cancels_reprice_that_would_exceed_lane_premium_cap(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(fill_after_orders=99, quote_bid=2.90, quote_ask=3.10)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[0],
            entry_reprice_cancel_after_seconds=1,
            entry_reprice_spread_pcts=[0.50],
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={"risk": deployment.risk.model_copy(update={"max_trade_premium_usd": 300.0})}
    )
    plan = TradePlan(
        trade_id="TRADE_REPRICE_CAP",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=1,
        estimated_entry_price=2.90,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY123",
    )

    result = asyncio.run(supervisor._wait_for_entry_fill_or_cancel(plan, deployment))

    assert result.cancelled_without_fill is True
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = json.loads(
            conn.execute("SELECT payload FROM events WHERE event_type = 'entry_reprice_blocked'").fetchone()[0]
        )
    assert payload["reason"] == "entry_reprice_above_max_trade_premium"


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


def test_entry_reprice_finalizes_confirmed_partial_fill_without_replacing_full_quantity(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(
        fill_after_orders=99,
        replacement_fill_price=2.87,
        cancel_status_after_cancel="CANCELED",
        cancel_filled_quantity=1,
        cancel_order_quantity=2,
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(entry_reprice_enabled=True),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={"risk": deployment.risk.model_copy(update={"max_trade_premium_usd": 1000.0})}
    )
    plan = TradePlan(
        trade_id="TRADE_ENTRY_PARTIAL_REPRICE",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=2,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_PARTIAL",
    )

    result = asyncio.run(supervisor._reprice_live_entry(plan, deployment, attempt=1))

    assert result.filled is True
    assert result.plan.quantity == 1
    assert result.plan.estimated_entry_price == 2.87
    assert result.plan.risk_details["entry_reprice_partial_fill"] is True
    assert order_manager.cancel_calls == ["ENTRY_PARTIAL"]
    assert order_manager.entry_calls == []


def test_final_entry_timeout_protects_confirmed_partial_fill(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(
        fill_after_orders=99,
        replacement_fill_price=2.87,
        cancel_status_after_cancel="CANCELED",
        cancel_filled_quantity=1,
        cancel_order_quantity=2,
    )
    planner = RecordingPlanner(order_manager)
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        app_config=AppConfig(
            order_fill_poll_seconds=0,
            entry_reprice_enabled=True,
            entry_reprice_checkpoints_seconds=[],
            entry_reprice_cancel_after_seconds=0,
        ),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    plan = TradePlan(
        trade_id="TRADE_ENTRY_PARTIAL_TIMEOUT",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=2,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_PARTIAL_TIMEOUT",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.quantity == 1
    assert protected.stop_order_id == "STOP123"
    assert planner.position_tracker.active_positions()[0].quantity == 1
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reprice_cancel_race_filled" in event_types
    assert "protective_stop_submission" in event_types


def test_entry_reprice_holds_reconciliation_for_overreported_fill_quantity(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    order_manager = RepricingOrderManager(
        fill_after_orders=99,
        cancel_status_after_cancel="CANCELED",
        cancel_filled_quantity=3,
        cancel_order_quantity=2,
    )
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(entry_reprice_enabled=True),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={"risk": deployment.risk.model_copy(update={"max_trade_premium_usd": 1000.0})}
    )
    plan = TradePlan(
        trade_id="TRADE_ENTRY_OVERREPORTED",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=2,
        estimated_entry_price=2.85,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_OVERREPORTED",
    )

    result = asyncio.run(supervisor._reprice_live_entry(plan, deployment, attempt=1))

    assert result.filled is False
    assert result.cancelled_without_fill is False
    assert result.error == "entry_reprice_cancel_unconfirmed:CANCELED"
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE event_type = 'entry_reprice_cancel_unconfirmed'"
            ).fetchone()[0]
        )
    assert payload["filled_quantity"] == 3
    assert payload["fill_quantity_ambiguous"] is True


def test_execution_supervisor_holds_reconciliation_when_cancel_status_receipt_is_delayed(tmp_path) -> None:
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

    assert protected.order_id == "ENTRY123"
    assert protected.stop_order_id is None
    assert order_manager.cancel_calls == ["ENTRY123"]
    assert order_manager.entry_calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reprice_cancel_unconfirmed" in event_types
    assert "entry_fill_timeout_reconcile" in event_types


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


@pytest.mark.parametrize("terminal_status", ["CANCELED", "CANCELLED"])
def test_execution_supervisor_protects_terminal_partial_entry_fill(
    tmp_path, terminal_status
) -> None:
    class TerminalPartialOrderManager(StubOrderManager):
        async def wait_for_fill(
            self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2
        ):
            del order_id, timeout_seconds, poll_seconds
            return (
                False,
                {
                    "status": terminal_status,
                    "filledQuantity": "1",
                    "averageFillPrice": "2.10",
                },
                terminal_status,
            )

    planner = StubPlanner()
    planner.order_manager = TerminalPartialOrderManager()
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_PARTIAL",
        option_symbol="QQQ260330P00558000",
        quantity=2,
        source="live_pending",
        order_id="ENTRY_PARTIAL",
    )
    plan = TradePlan(
        trade_id="TRADE_PARTIAL",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        direction=SignalDirection.SHORT,
        option_symbol="QQQ260330P00558000",
        quantity=2,
        estimated_entry_price=2.0,
        risk_reasons=["approved"],
        dry_run=False,
        order_id="ENTRY_PARTIAL",
    )

    protected = asyncio.run(supervisor._protect_live_entry(plan, deployment))

    assert protected.quantity == 1
    assert protected.estimated_entry_price == 2.10
    assert protected.stop_order_id == "STOP123"
    assert planner.position_tracker.active_positions()[0].quantity == 1
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_terminal_partial_fill_recovered" in event_types
    assert "protective_stop_submission" in event_types


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
    class FilledOrderManager(StubOrderManager):
        async def get_order_status(self, order_id: str):
            del order_id
            return (
                "FILLED",
                {"status": "FILLED", "quantity": "1", "filledQuantity": "1", "averagePrice": "2.0"},
                None,
            )

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    planner = StubPlanner()
    planner.order_manager = FilledOrderManager()
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


def test_sync_lifecycle_keeps_active_partial_entry_on_hold_until_order_terminal(tmp_path) -> None:
    class PartialEntryOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.status = "PARTIALLY_FILLED"
            self.filled_quantity = "1"

        async def get_order_status(self, order_id: str):
            del order_id
            return (
                self.status,
                {
                    "status": self.status,
                    "quantity": "2",
                    "filledQuantity": self.filled_quantity,
                    "averagePrice": "2.10",
                },
                None,
            )

    manager = PartialEntryOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    planner.cash_guard = RecordingCashGuard()
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    trade = TradeRecord(
        trade_id="TRADE_PARTIAL_ACTIVE",
        deployment_id="market_impulse_qqq_short_v1",
        symbol="QQQ",
        option_symbol="QQQ260330P00558000",
        quantity=2,
        entry_price=2.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        status="pending_entry_reconcile",
        entry_order_id="ENTRY_PARTIAL_ACTIVE",
    )
    asyncio.run(trade_repo.upsert_trade(trade))
    planner.position_tracker.open_position(
        "QQQ",
        trade.deployment_id,
        trade_id=trade.trade_id,
        option_symbol=trade.option_symbol,
        quantity=1,
        entry_price=2.10,
        entry_timestamp=trade.entry_timestamp,
        source="live_open",
        order_id=trade.entry_order_id,
        stop_order_id="STOP_PARTIAL",
        stop_price=1.05,
    )

    asyncio.run(supervisor.sync_lifecycle())

    held = asyncio.run(trade_repo.get_open_trades())[0]
    assert held.status == "pending_entry_reconcile"
    assert held.quantity == 2
    assert held.stop_order_id == "STOP_PARTIAL"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reconcile_recovered" not in event_types

    # The residual lot can fill while cancellation is still settling. The
    # durable hold kept the original order quantity (2), so this is valid.
    manager.status = "FILLED"
    manager.filled_quantity = "2"
    asyncio.run(supervisor.sync_lifecycle())

    recovered = asyncio.run(trade_repo.get_open_trades())[0]
    assert recovered.status == "open_protected"
    assert recovered.quantity == 2
    assert recovered.stop_order_id == "STOP_PARTIAL"


def test_reconcile_hold_position_arms_only_catastrophe_protection(tmp_path) -> None:
    class HoldOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.quote_calls = 0

        async def get_portfolio(self):
            return {"orders": []}

        async def get_option_quote(self, option_symbol: str):
            del option_symbol
            self.quote_calls += 1
            raise AssertionError("reconciliation hold must not evaluate target/profile exits")

    manager = HoldOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_HOLD_PROTECT",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.10,
        source=LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE,
        order_id="ENTRY_HOLD_PROTECT",
    )
    position = planner.position_tracker.active_positions()[0]

    protected = asyncio.run(supervisor.manage_open_position(deployment, position, dry_run=False))

    assert protected is not None
    assert protected.stop_order_id == "STOP123"
    assert manager.quote_calls == 0
    assert planner.position_tracker.active_positions()[0].stop_order_id == "STOP123"


def test_reconcile_hold_resizes_confirmed_stale_protection_quantity(tmp_path) -> None:
    class MismatchedProtectionOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.cancel_calls: list[str] = []
            self.stop_quantities: list[int] = []

        async def get_portfolio(self):
            return {
                "orders": [
                    {
                        "orderId": "STOP_ONE_LOT",
                        "instrument": {"symbol": "QQQ260330P00558000", "type": "OPTION"},
                        "side": "SELL",
                        "openCloseIndicator": "CLOSE",
                        "type": "STOP",
                        "status": "NEW",
                        "quantity": "1",
                        "filledQuantity": None,
                        "stopPrice": "1.05",
                    }
                ]
            }

        async def cancel_order(self, order_id: str):
            self.cancel_calls.append(order_id)
            return True, None

        async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
            del option_symbol, stop_price
            self.stop_quantities.append(quantity)
            return OrderResult(order_id="STOP_TWO_LOTS")

    manager = MismatchedProtectionOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(planner=planner, event_repository=repo)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_RESIZE_PROTECT",
        option_symbol="QQQ260330P00558000",
        quantity=2,
        entry_price=2.10,
        source=LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE,
        order_id="ENTRY_RESIZE_PROTECT",
    )

    protected = asyncio.run(
        supervisor.manage_open_position(deployment, planner.position_tracker.active_positions()[0], dry_run=False)
    )

    assert protected is not None and protected.stop_order_id == "STOP_TWO_LOTS"
    assert manager.cancel_calls == ["STOP_ONE_LOT"]
    assert manager.stop_quantities == [2]


def test_reconcile_hold_keeps_stale_protection_while_resize_cancel_pending(tmp_path) -> None:
    class PendingResizeOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.stop_calls = 0

        async def get_portfolio(self):
            return {
                "orders": [
                    {
                        "orderId": "STOP_ONE_LOT",
                        "instrument": {"symbol": "QQQ260330P00558000", "type": "OPTION"},
                        "side": "SELL",
                        "openCloseIndicator": "CLOSE",
                        "type": "STOP",
                        "status": "PENDING_CANCEL",
                        "quantity": "1",
                        "filledQuantity": None,
                        "stopPrice": "1.05",
                    }
                ]
            }

        async def cancel_order(self, order_id: str):
            del order_id
            return False, "cancel_pending:PENDING_CANCEL"

        async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int):
            del option_symbol, stop_price, quantity
            self.stop_calls += 1
            return OrderResult(order_id="UNSAFE_DUPLICATE_STOP")

    manager = PendingResizeOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(planner=planner, event_repository=repo)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_PENDING_RESIZE",
        option_symbol="QQQ260330P00558000",
        quantity=2,
        entry_price=2.10,
        source=LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE,
        order_id="ENTRY_PENDING_RESIZE",
    )

    protected = asyncio.run(
        supervisor.manage_open_position(deployment, planner.position_tracker.active_positions()[0], dry_run=False)
    )

    assert protected is not None and protected.stop_order_id == "STOP_ONE_LOT"
    assert manager.stop_calls == 0
    with sqlite3.connect(tmp_path / "events.db") as conn:
        issues = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM events WHERE event_type = 'runtime_issue'")
        ]
    assert any(issue["category"] == "protection_quantity_mismatch" for issue in issues)


def test_hard_flat_defers_partial_entry_hold_until_entry_order_terminal(tmp_path) -> None:
    class PendingEntryCancelOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.close_calls = 0

        async def cancel_order(self, order_id: str):
            del order_id
            return False, "cancel_pending:PENDING_CANCEL"

        async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
            del option_symbol, quantity, exit_mode, limit_price
            self.close_calls += 1
            return OrderResult(order_id="UNSAFE_HARD_FLAT")

    manager = PendingEntryCancelOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(planner=planner, event_repository=repo)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_HARD_FLAT_HOLD",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.10,
        source=LIVE_ENTRY_RECONCILIATION_HOLD_SOURCE,
        order_id="ENTRY_HARD_FLAT_HOLD",
        stop_order_id="STOP_HARD_FLAT_HOLD",
        stop_price=1.05,
    )

    plans = asyncio.run(
        supervisor.close_due_positions(
            {deployment.deployment_id: deployment},
            now=datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
            dry_run=False,
        )
    )

    assert plans == []
    assert manager.close_calls == 0
    assert planner.position_tracker.active_positions()[0].stop_order_id == "STOP_HARD_FLAT_HOLD"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        issues = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM events WHERE event_type = 'runtime_issue'")
        ]
    assert any(issue["category"] == "entry_reconcile_flatten_deferred" for issue in issues)


def test_sync_lifecycle_does_not_close_live_position_on_zero_fill_order_conflict(tmp_path) -> None:
    class ConflictingOrderManager(StubOrderManager):
        async def get_order_status(self, order_id: str):
            del order_id
            return "CANCELLED", {"status": "CANCELLED", "filledQuantity": None}, None

    planner = StubPlanner()
    planner.order_manager = ConflictingOrderManager()
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    trade = TradeRecord(
        trade_id="TRADE_CONFLICT",
        deployment_id="market_impulse_qqq_short_v1",
        symbol="QQQ",
        option_symbol="QQQ260330P00558000",
        quantity=1,
        entry_price=2.0,
        status="pending_entry_reconcile",
        entry_order_id="ENTRY_CONFLICT",
    )
    asyncio.run(trade_repo.upsert_trade(trade))
    planner.position_tracker.open_position(
        "QQQ",
        trade.deployment_id,
        trade_id=trade.trade_id,
        option_symbol=trade.option_symbol,
        quantity=1,
        entry_price=2.0,
        source="live_open",
        order_id=trade.entry_order_id,
    )

    asyncio.run(supervisor.sync_lifecycle())

    held = asyncio.run(trade_repo.get_open_trades())[0]
    assert held.status == "pending_entry_reconcile"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reconcile_position_order_conflict" in event_types
    assert "entry_reconcile_released" not in event_types


@pytest.mark.parametrize("terminal_status", ["CANCELED", "CANCELLED"])
def test_execution_supervisor_sync_lifecycle_releases_terminal_reconcile_hold(
    tmp_path, terminal_status
) -> None:
    class CanceledOrderManager(StubOrderManager):
        async def get_order_status(self, order_id: str):
            del order_id
            return terminal_status, {"status": terminal_status}, None

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


def test_execution_supervisor_recovers_and_protects_terminal_reconcile_fill(tmp_path) -> None:
    class PartiallyFilledCanceledOrderManager(StubOrderManager):
        async def get_order_status(self, order_id: str):
            del order_id
            return (
                "CANCELLED",
                {"status": "CANCELLED", "filledQuantity": "1", "averageFillPrice": "2.10"},
                None,
            )

    class ReconcilePlanner(StubPlanner):
        def __init__(self):
            super().__init__()
            self.order_manager = PartiallyFilledCanceledOrderManager()
            self.cash_guard = RecordingCashGuard()

    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=ReconcilePlanner(),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )

    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_PARTIAL",
                deployment_id="market_impulse_qqq_short_v1",
                symbol="QQQ",
                option_symbol="QQQ260330P00558000",
                quantity=2,
                entry_price=2.0,
                status="pending_entry_reconcile",
                entry_order_id="ENTRY_PARTIAL",
            )
        )
    )
    asyncio.run(supervisor.sync_lifecycle())

    recovered = supervisor.planner.position_tracker.active_positions()[0]
    assert recovered.quantity == 1
    assert recovered.entry_price == 2.10
    base = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = base.model_copy(
        update={"execution": base.execution.model_copy(update={"shadow_only": True})}
    )
    protected = asyncio.run(supervisor.manage_open_position(deployment, recovered, dry_run=False))
    assert protected is not None
    assert protected.stop_order_id == "STOP123"
    assert supervisor.planner.cash_guard.calls == []
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "entry_reconcile_terminal_fill_recovered" in event_types
    assert "protective_stop_submission" in event_types
    assert "entry_reconcile_released" not in event_types


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


def test_algorithmic_exit_blocks_while_protection_cancel_is_unconfirmed(tmp_path) -> None:
    class PendingProtectionCancelOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.close_calls = 0

        async def cancel_order(self, order_id: str):
            del order_id
            return False, "cancel_pending:PENDING_CANCEL"

        async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
            del option_symbol, quantity, exit_mode, limit_price
            self.close_calls += 1
            return OrderResult(order_id="UNSAFE_CLOSE")

    manager = PendingProtectionCancelOrderManager()
    planner = StubPlanner()
    planner.order_manager = manager
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=planner,
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_CANCEL_PENDING",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_PENDING_CANCEL",
        stop_price=1.0,
    )
    position = planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 7, 16, 15, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_exit"],
        cancel_protection_orders=True,
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is not None
    assert plan.order_id is None
    assert plan.error == "exit_cancel_confirmation_pending:cancel_pending:PENDING_CANCEL"
    assert manager.close_calls == 0
    tracked = planner.position_tracker.active_positions()[0]
    assert tracked.stop_order_id == "STOP_PENDING_CANCEL"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert "exit_cancel_confirmation_blocked" in event_types
    assert "exit_submission" not in event_types


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


class ExitRepriceCancelRaceOrderManager(StubOrderManager):
    """Reproduces the 2026-07-08 AMD live gap (workplan hygiene item A).

    The FIRST exit order (``CLOSE_RACE_1``) genuinely fills at the broker, but
    the pending-exit poller's status check lands one beat before that fill is
    visible ("NEW"). The poller then sees the quote has moved and tries to
    reprice: it cancels ``CLOSE_RACE_1`` -- but the broker refuses the cancel
    because the order already filled (an "ambiguous cancel", reported here as
    ``canceled=False``). Any duplicate close order the (unfixed) resubmit path
    might place on top of an already-flat position is rejected by the broker,
    exactly as production would reject it.
    """

    def __init__(self) -> None:
        self.quote_calls = 0
        self.status_calls: dict[str, int] = {}
        self.cancel_calls: list[str] = []
        self.close_orders_placed: list[str] = []
        self._next_close_id = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, quantity, exit_mode, limit_price
        self._next_close_id += 1
        order_id = f"CLOSE_RACE_{self._next_close_id}"
        self.close_orders_placed.append(order_id)

        class Result:
            pass

        result = Result()
        result.order_id = order_id
        result.error = None
        return result

    async def get_option_quote(self, option_symbol):
        self.quote_calls += 1
        # Submission quote: bid 13.05. Reprice-check quote (and every quote
        # after): bid moved to 13.55 -- a material change that arms the
        # STRATEGY reprice branch.
        bid = 13.05 if self.quote_calls == 1 else 13.55
        return PublicQuote(
            symbol=option_symbol,
            bid=bid,
            ask=bid + 0.05,
            last=bid + 0.02,
            open_interest=200,
            outcome="SUCCESS",
        )

    async def get_order_status(self, order_id):
        self.status_calls[order_id] = self.status_calls.get(order_id, 0) + 1
        if order_id == "CLOSE_RACE_1":
            if self.status_calls[order_id] == 1:
                return "NEW", {"status": "NEW"}, None
            return (
                "FILLED",
                {
                    "orderId": "CLOSE_RACE_1",
                    "instrument": {"symbol": "AMD260713P00500000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "13.55",
                    "closedAt": "2026-07-08T14:05:00Z",
                },
                None,
            )
        # A resubmitted duplicate close on an already-flat position: the
        # broker rejects it (there is nothing left to sell).
        return "REJECTED", {"status": "REJECTED"}, None

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        if order_id == "CLOSE_RACE_1":
            # The broker refuses to cancel -- it already filled.
            return False, "order_already_filled"
        return True, None


def test_execution_supervisor_exit_reprice_cancel_race_persists_fill_truth_for_target_1_partial_square_off(
    tmp_path,
) -> None:
    """Item A (2026-07-08 hygiene batch): a 1-lot position where the profile's
    ``target_1_partial`` rule fully banks the position (``_partial_quantity``
    rounds up to the full quantity, so the FSM action is a full ``square_off``,
    not a partial scale -- this mapping is by design). The exit fills, but a
    reprice tick races the fill's cancel-confirmation. Before the fix, the
    code blindly resubmitted a new close order on the now-flat position,
    orphaning the ``exit_order_id`` that actually carried the fill -- so
    ``exit_price``/``exit_filled_quantity``/``exit_filled_at`` never wrote
    back even though the trade correctly reached ``status=closed`` and
    ``exit_rule`` (persisted at submission time) survived. This mirrors the
    real AMD deployment
    (strategy_expand30_amd_mi_01_amd_short_live_row_2, entry 10.40,
    AMD260713P00500000).
    """
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = ExitRepriceCancelRaceOrderManager()
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
                trade_id="TRADE_AMD",
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=1,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 8, 13, 45, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_AMD",
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id="TRADE_AMD",
        option_symbol="AMD260713P00500000",
        quantity=1,
        entry_price=10.40,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    # Mirrors profile_decision_to_exit_decision's real output for a 1-lot
    # target_1_partial bank (profile_exit.py: bank_qty >= quantity -> the
    # PARTIAL_SCALE branch is skipped and _full_exit fires with
    # fsm_action=SQUARE_OFF). No "partial_scale" feature key -- this is a
    # plain full square-off, not a partial scale.
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="AMD",
        timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_target_1_full"],
        cancel_protection_orders=True,
        features={
            "profile_id": "amd_short_live",
            "profile_rule": "target_1_partial",
            "profile_fsm_action": "square_off",
            "exit_quantity": 1,
        },
    )

    submit_plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert submit_plan is not None
    assert submit_plan.order_id == "CLOSE_RACE_1"

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    assert len(plans) == 1
    # The fix must resolve the race on a single poll -- no duplicate close
    # order is ever placed on the now-flat position.
    assert order_manager.close_orders_placed == ["CLOSE_RACE_1"]
    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at, exit_rule
            FROM trade_sessions
            WHERE trade_id = 'TRADE_AMD'
            """
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert row == (
        "closed",
        "CLOSE_RACE_1",
        13.55,
        1,
        "2026-07-08T14:05:00+00:00",
        "target_1_partial",
    )
    assert "exit_reprice_cancel_race_filled" in event_types


class ExitRepricePartialCancelRaceOrderManager(StubOrderManager):
    """Audit fix A.2 repro: a 2-lot exit order raced by a reprice cancel.

    The cancel SUCCEEDS at the broker, but the readback shows the common
    real-broker outcome: CANCELED with filledQuantity=1 of 2 (avg 13.10) --
    one lot filled before the cancel took effect. Pre-fix, the reprice path
    resubmitted for the STALE FULL quantity (2), attempting to oversell, and
    the 1-lot fill's price was permanently lost (trade_partial_fills only
    covered deliberate _handle_partial_scale_locked banks).
    """

    def __init__(self) -> None:
        self.quote_calls = 0
        self.status_calls: dict[str, int] = {}
        self.close_orders: list[tuple[str, int]] = []
        self._next = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, exit_mode, limit_price
        self._next += 1
        order_id = f"CLOSE_PART_{self._next}"
        self.close_orders.append((order_id, int(quantity)))
        return OrderResult(order_id=order_id)

    async def get_option_quote(self, option_symbol):
        self.quote_calls += 1
        bid = 13.05 if self.quote_calls == 1 else 13.55
        return PublicQuote(symbol=option_symbol, bid=bid, ask=bid + 0.05, last=bid + 0.02, open_interest=200, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        self.status_calls[order_id] = self.status_calls.get(order_id, 0) + 1
        if order_id == "CLOSE_PART_1":
            if self.status_calls[order_id] == 1:
                return "NEW", {"status": "NEW"}, None
            return (
                "CANCELED",
                {
                    "orderId": "CLOSE_PART_1",
                    "instrument": {"symbol": "AMD260713P00500000", "type": "OPTION"},
                    "status": "CANCELED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "13.10",
                    "closedAt": "2026-07-08T14:05:00Z",
                },
                None,
            )
        return "NEW", {"status": "NEW"}, None

    async def cancel_order(self, order_id):
        del order_id
        return True, None


def test_exit_reprice_cancel_race_partial_fill_records_leg_and_resubmits_residual_only(tmp_path) -> None:
    """Audit fix A.2 (auditor's exact scenario, failing-first): 2-lot exit,
    cancel succeeds, readback CANCELED/filledQuantity=1/avg 13.10. The fix
    must (a) durably record the 1-lot fill in trade_partial_fills with
    origin="exit_cancel_race" and (b) resubmit for the RESIDUAL quantity (1)
    only -- never the stale full 2 (an oversell attempt)."""
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = ExitRepricePartialCancelRaceOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_AMD_PART",
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=2,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 8, 13, 45, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_AMD2",
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id="TRADE_AMD_PART",
        option_symbol="AMD260713P00500000",
        quantity=2,
        entry_price=10.40,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="AMD",
        timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_max_hold:3600s"],
        cancel_protection_orders=True,
    )

    submit_plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert submit_plan is not None
    assert order_manager.close_orders == [("CLOSE_PART_1", 2)]

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # (b) NO OVERSELL: the resubmit is sized to the residual (1), never the
    # stale full quantity (2). Pre-fix this was [(CLOSE_PART_1, 2), (CLOSE_PART_2, 2)].
    assert order_manager.close_orders == [("CLOSE_PART_1", 2), ("CLOSE_PART_2", 1)]
    assert len(plans) == 1
    assert plans[0].quantity == 1

    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 1
    assert tracked.exit_order_id == "CLOSE_PART_2"

    with sqlite3.connect(tmp_path / "events.db") as conn:
        partial_rows = conn.execute(
            """
            SELECT trade_id, closed_quantity, order_id, fill_price, fill_quantity, filled_at, order_status, origin
            FROM trade_partial_fills
            WHERE trade_id = 'TRADE_AMD_PART'
            """
        ).fetchall()
        trade_row = conn.execute(
            "SELECT status, quantity, exit_order_id FROM trade_sessions WHERE trade_id = 'TRADE_AMD_PART'"
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]

    # (a) The raced 1-lot fill's economics are durably recorded.
    assert partial_rows == [
        (
            "TRADE_AMD_PART",
            1,
            "CLOSE_PART_1",
            13.10,
            1,
            "2026-07-08T14:05:00+00:00",
            "CANCELED",
            "exit_cancel_race",
        )
    ]
    assert trade_row == ("exit_pending", 1, "CLOSE_PART_2")
    assert "exit_cancel_race_partial_fill" in event_types


class ExitRepriceBlockedReadbackOrderManager(StubOrderManager):
    """Audit fix A.1 repro: the reprice cancel fails AND the status readback
    fails (broker 5xx/slow -- ``get_order_status`` swallows exceptions into an
    error string, it never raises). The old order's state is unknown: it may
    still fill. Pre-fix the code blindly resubmitted anyway. On the SECOND
    manage tick the broker recovers and reports the old order FILLED."""

    allows_exit_submission_before_cancel_confirmation = True

    def __init__(self) -> None:
        self.quote_calls = 0
        self.status_calls: dict[str, int] = {}
        self.close_orders: list[tuple[str, int]] = []
        self._next = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, exit_mode, limit_price
        self._next += 1
        order_id = f"CLOSE_BLK_{self._next}"
        self.close_orders.append((order_id, int(quantity)))
        return OrderResult(order_id=order_id)

    async def get_option_quote(self, option_symbol):
        self.quote_calls += 1
        bid = 13.05 if self.quote_calls == 1 else 13.55
        return PublicQuote(symbol=option_symbol, bid=bid, ask=bid + 0.05, last=bid + 0.02, open_interest=200, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        self.status_calls[order_id] = self.status_calls.get(order_id, 0) + 1
        if order_id == "CLOSE_BLK_1":
            calls = self.status_calls[order_id]
            if calls == 1:
                return "NEW", {"status": "NEW"}, None
            if calls == 2:
                # The readback after the failed cancel: broker still degraded.
                return None, None, "broker 503 readback failed"
            return (
                "FILLED",
                {
                    "orderId": "CLOSE_BLK_1",
                    "instrument": {"symbol": "AMD260713P00500000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "13.60",
                    "closedAt": "2026-07-08T14:06:00Z",
                },
                None,
            )
        return None, None, "unknown_order"

    async def cancel_order(self, order_id):
        del order_id
        return False, "cancel http 503"


def test_exit_reprice_blocked_when_cancel_unconfirmed_and_readback_fails_then_recovers(tmp_path) -> None:
    """Audit fix A.1 (fail closed): cancel not clean-confirmed + readback did
    not cleanly return a terminal status -> NO resubmit this cycle (the order
    may still fill; a blind resubmit is a potential double-sell). The
    discarded status_error must be logged (exit_reprice_blocked event). The
    next manage tick retries and, when the broker recovers reporting FILLED,
    the full fill truth is written back."""
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = ExitRepriceBlockedReadbackOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_AMD_BLK",
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=1,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 8, 13, 45, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_AMD3",
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id="TRADE_AMD_BLK",
        option_symbol="AMD260713P00500000",
        quantity=1,
        entry_price=10.40,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="AMD",
        timestamp=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_max_hold:3600s"],
        cancel_protection_orders=True,
    )

    asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert order_manager.close_orders == [("CLOSE_BLK_1", 1)]

    # Tick 1: cancel fails + readback fails -> BLOCKED, nothing resubmitted.
    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    assert plans == []
    assert order_manager.close_orders == [("CLOSE_BLK_1", 1)]  # no blind resubmit
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.exit_order_id == "CLOSE_BLK_1"  # true order id NOT orphaned

    with sqlite3.connect(tmp_path / "events.db") as conn:
        blocked_events = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM events WHERE event_type = 'exit_reprice_blocked'"
            ).fetchall()
        ]
    assert len(blocked_events) == 1
    assert blocked_events[0]["status_error"] == "broker 503 readback failed"
    assert blocked_events[0]["cancel_error"] == "cancel http 503"

    # Tick 2: broker recovered; the routine status poll sees FILLED and the
    # fill truth is written back -- the fail-safe recovers on its own.
    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    assert len(plans) == 1
    assert order_manager.close_orders == [("CLOSE_BLK_1", 1)]
    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at
            FROM trade_sessions
            WHERE trade_id = 'TRADE_AMD_BLK'
            """
        ).fetchone()
    assert row == ("closed", "CLOSE_BLK_1", 13.60, 1, "2026-07-08T14:06:00+00:00")


# --------------------------------------------------------------------------- #
# Audit fix 3: partial-fill enrichment sweep hardening -- per-call timeout and
# a durable give-up state so a degraded broker cannot stall reconciliation or
# be re-polled forever.
# --------------------------------------------------------------------------- #


def _seed_partial_fill_record(trade_repo, *, order_id: str) -> int:
    from bhiksha.domain.models import PartialFillRecord

    return asyncio.run(
        trade_repo.record_partial_fill(
            PartialFillRecord(
                id=None,
                trade_id="TRADE_SWEEP",
                deployment_id="qqq_live",
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                closed_quantity=1,
                order_id=order_id,
                exit_rule="target_1_partial",
                submitted_at=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),
            )
        )
    )


def test_partial_fill_enrich_sweep_times_out_slow_broker_and_counts_attempt(tmp_path) -> None:
    """A hung/slow get_order_status (broker client timeout is ~30s) must not
    stall the sweep -- and therefore reconciliation, which runs it under
    sync_lock -- beyond the 1s per-call readback timeout. The row survives
    unresolved with one attempt counted."""
    import time

    class SlowStatusOrderManager(StubOrderManager):
        async def get_order_status(self, order_id):
            await asyncio.sleep(5)
            return "FILLED", {"status": "FILLED"}, None

    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(SlowStatusOrderManager()),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="SLOW_1")

    started = time.monotonic()
    asyncio.run(supervisor._enrich_pending_partial_fills())
    elapsed = time.monotonic() - started
    assert elapsed < 3.0  # 1s wait_for, not the 5s broker stall

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT fill_price, enrich_attempts, abandoned_reason FROM trade_partial_fills WHERE order_id = 'SLOW_1'"
        ).fetchone()
    assert row == (None, 1, None)  # unresolved, one attempt counted, not abandoned


def test_partial_fill_enrich_sweep_abandons_on_terminal_dead_status_without_fill(tmp_path) -> None:
    """An order that reads back terminally dead with no fill can never be
    enriched: abandon it with a reason instead of re-polling forever."""

    class RejectedStatusOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.calls = 0

        async def get_order_status(self, order_id):
            self.calls += 1
            return "REJECTED", {"status": "REJECTED"}, None

    order_manager = RejectedStatusOrderManager()
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="DEAD_1")

    asyncio.run(supervisor._enrich_pending_partial_fills())

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT abandoned_reason FROM trade_partial_fills WHERE order_id = 'DEAD_1'"
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events").fetchall()]
    assert row == ("terminal_status:REJECTED",)
    assert "partial_fill_enrich_abandoned" in event_types

    # Abandoned rows are excluded from future sweeps -- no more polls.
    calls_after_abandon = order_manager.calls
    asyncio.run(supervisor._enrich_pending_partial_fills())
    assert order_manager.calls == calls_after_abandon


def test_partial_fill_enrich_sweep_gives_up_after_max_attempts(tmp_path) -> None:
    """A row whose status poll keeps failing (degraded broker) is abandoned
    with a reason after _PARTIAL_FILL_ENRICH_MAX_ATTEMPTS unresolved polls."""
    from bhiksha.execution.supervisor import _PARTIAL_FILL_ENRICH_MAX_ATTEMPTS

    class AlwaysErroringStatusOrderManager(StubOrderManager):
        def __init__(self) -> None:
            self.calls = 0

        async def get_order_status(self, order_id):
            self.calls += 1
            return None, None, "boom"

    order_manager = AlwaysErroringStatusOrderManager()
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="ERR_1")

    for _ in range(_PARTIAL_FILL_ENRICH_MAX_ATTEMPTS):
        asyncio.run(supervisor._enrich_pending_partial_fills())

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT abandoned_reason FROM trade_partial_fills WHERE order_id = 'ERR_1'"
        ).fetchone()
    assert row == ("max_poll_attempts:boom",)
    assert order_manager.calls == _PARTIAL_FILL_ENRICH_MAX_ATTEMPTS

    # And no further polls once abandoned.
    asyncio.run(supervisor._enrich_pending_partial_fills())
    assert order_manager.calls == _PARTIAL_FILL_ENRICH_MAX_ATTEMPTS


def test_partial_fill_enrich_sweep_records_partial_truth_from_dead_order(tmp_path) -> None:
    """A deliberate bank whose close order died AFTER filling part of the leg
    still gets its available truth recorded (CANCELED + filledQuantity>0),
    rather than being abandoned as if unfilled."""

    class CanceledWithFillOrderManager(StubOrderManager):
        async def get_order_status(self, order_id):
            return (
                "CANCELED",
                {
                    "orderId": order_id,
                    "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                    "status": "CANCELED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "3.05",
                    "closedAt": "2026-07-08T14:12:00Z",
                },
                None,
            )

    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(CanceledWithFillOrderManager()),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="DEADFILL_1")

    asyncio.run(supervisor._enrich_pending_partial_fills())

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT fill_price, fill_quantity, order_status, abandoned_reason FROM trade_partial_fills WHERE order_id = 'DEADFILL_1'"
        ).fetchone()
    assert row == (3.05, 1, "CANCELED", None)


# --------------------------------------------------------------------------- #
# Item #21: filledQuantity guard on the THIRD resubmit site -- the dead-status
# (REJECTED/CANCELED/EXPIRED) branch of _manage_pending_exit_locked. Mirrors
# the reprice-cancel guard (_resolve_exit_cancel_for_reprice) so a partial fill
# that a routine poll discovers on a now-dead order can never be resubmitted at
# the stale full quantity (an oversell).
# --------------------------------------------------------------------------- #


class ExitDeadStatusPartialFillOrderManager(StubOrderManager):
    """Item #21 repro: a resting 2-lot exit order that a routine poll finds
    DEAD (CANCELED) AFTER one lot already filled (avg 13.10). Pre-fix the
    dead-status branch resubmitted the STALE FULL quantity (2) -- an oversell
    -- and permanently lost the 1-lot fill's price. The fix must record the
    filled leg (origin="exit_dead_status") and resubmit the RESIDUAL (1) only."""

    def __init__(self) -> None:
        self.status_calls: dict[str, int] = {}
        self.close_orders: list[tuple[str, int]] = []
        self._next = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, exit_mode, limit_price
        self._next += 1
        order_id = f"CLOSE_DEAD_{self._next}"
        self.close_orders.append((order_id, int(quantity)))
        return OrderResult(order_id=order_id)

    async def get_option_quote(self, option_symbol):
        return PublicQuote(symbol=option_symbol, bid=13.05, ask=13.10, last=13.07, open_interest=200, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        self.status_calls[order_id] = self.status_calls.get(order_id, 0) + 1
        if order_id == "CLOSE_DEAD_1":
            return (
                "CANCELED",
                {
                    "orderId": "CLOSE_DEAD_1",
                    "instrument": {"symbol": "AMD260713P00500000", "type": "OPTION"},
                    "status": "CANCELED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "13.10",
                    "closedAt": "2026-07-09T14:05:00Z",
                },
                None,
            )
        return "NEW", {"status": "NEW"}, None

    async def cancel_order(self, order_id):
        del order_id
        return True, None


def test_exit_dead_status_partial_fill_records_leg_and_resubmits_residual_only(tmp_path) -> None:
    """Item #21 (failing-first): a 2-lot exit order polls back CANCELED with
    filledQuantity=1/avg 13.10. The fix must (a) durably record the 1-lot fill
    in trade_partial_fills with origin="exit_dead_status" and (b) resubmit for
    the RESIDUAL (1) only -- never the stale full 2 (an oversell attempt)."""
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = ExitDeadStatusPartialFillOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_AMD_DEAD",
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=2,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 9, 13, 45, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_AMD_DEAD",
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id="TRADE_AMD_DEAD",
        option_symbol="AMD260713P00500000",
        quantity=2,
        entry_price=10.40,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="AMD",
        timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_max_hold:3600s"],
        cancel_protection_orders=True,
    )

    submit_plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert submit_plan is not None
    assert order_manager.close_orders == [("CLOSE_DEAD_1", 2)]

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # (b) NO OVERSELL: the resubmit is sized to the residual (1), never the
    # stale full quantity (2). Pre-fix this was [(CLOSE_DEAD_1, 2), (CLOSE_DEAD_2, 2)].
    assert order_manager.close_orders == [("CLOSE_DEAD_1", 2), ("CLOSE_DEAD_2", 1)]
    assert len(plans) == 1
    assert plans[0].quantity == 1

    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 1
    assert tracked.exit_order_id == "CLOSE_DEAD_2"

    with sqlite3.connect(tmp_path / "events.db") as conn:
        partial_rows = conn.execute(
            """
            SELECT trade_id, closed_quantity, order_id, fill_price, fill_quantity, filled_at, order_status, origin
            FROM trade_partial_fills
            WHERE trade_id = 'TRADE_AMD_DEAD'
            """
        ).fetchall()
        trade_row = conn.execute(
            "SELECT status, quantity, exit_order_id FROM trade_sessions WHERE trade_id = 'TRADE_AMD_DEAD'"
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]

    # (a) The raced 1-lot fill's economics are durably recorded, tagged with
    # the item-#21 origin (distinct from the reprice "exit_cancel_race" leg).
    assert partial_rows == [
        (
            "TRADE_AMD_DEAD",
            1,
            "CLOSE_DEAD_1",
            13.10,
            1,
            "2026-07-09T14:05:00+00:00",
            "CANCELED",
            "exit_dead_status",
        )
    ]
    assert trade_row == ("exit_pending", 1, "CLOSE_DEAD_2")
    assert "exit_dead_status_partial_fill" in event_types


class ExitDeadStatusFullFillOrderManager(StubOrderManager):
    """Item #21 defensive branch: a 1-lot exit order reads back DEAD (CANCELED)
    but with filledQuantity covering the WHOLE position -- it actually filled
    before dying. The fix must finalize the fill truth and NEVER resubmit a
    duplicate close (which would orphan the true exit_order_id)."""

    def __init__(self) -> None:
        self.close_orders: list[tuple[str, int]] = []
        self._next = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, exit_mode, limit_price
        self._next += 1
        order_id = f"CLOSE_FULL_{self._next}"
        self.close_orders.append((order_id, int(quantity)))
        return OrderResult(order_id=order_id)

    async def get_option_quote(self, option_symbol):
        return PublicQuote(symbol=option_symbol, bid=13.55, ask=13.60, last=13.57, open_interest=200, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        if order_id == "CLOSE_FULL_1":
            return (
                "CANCELED",
                {
                    "orderId": "CLOSE_FULL_1",
                    "instrument": {"symbol": "AMD260713P00500000", "type": "OPTION"},
                    "status": "CANCELED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "13.55",
                    "closedAt": "2026-07-09T14:06:00Z",
                },
                None,
            )
        return "NEW", {"status": "NEW"}, None

    async def cancel_order(self, order_id):
        del order_id
        return True, None


def test_exit_dead_status_fully_filled_finalizes_and_does_not_resubmit(tmp_path) -> None:
    """Item #21: a dead status whose filledQuantity covers the full position is
    a completed exit -- finalize (record fill truth, mark trade closed) and
    place NO second close order."""
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = ExitDeadStatusFullFillOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_AMD_FULL",
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol="AMD260713P00500000",
                quantity=1,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 9, 13, 45, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_AMD_FULL",
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id="TRADE_AMD_FULL",
        option_symbol="AMD260713P00500000",
        quantity=1,
        entry_price=10.40,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="AMD",
        timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_max_hold:3600s"],
        cancel_protection_orders=True,
    )

    asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))
    assert order_manager.close_orders == [("CLOSE_FULL_1", 1)]

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # NO resubmit: the dead order already filled the whole position.
    assert order_manager.close_orders == [("CLOSE_FULL_1", 1)]
    assert len(plans) == 1
    assert supervisor.planner.position_tracker.active_positions() == []

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at
            FROM trade_sessions
            WHERE trade_id = 'TRADE_AMD_FULL'
            """
        ).fetchone()
        event_types = [event[0] for event in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]
    assert row == ("closed", "CLOSE_FULL_1", 13.55, 1, "2026-07-09T14:06:00+00:00")
    assert "exit_dead_status_filled" in event_types


def test_partial_fill_abandonment_escalates_to_runtime_issue_in_daily_report(tmp_path) -> None:
    """Item #22: a permanently-abandoned partial leg is silent fill-detail loss
    that needs a human backfill. Besides the diagnostic
    ``partial_fill_enrich_abandoned`` row, the abandonment must escalate as a
    ``runtime_issue`` so daily_report aggregates it into ``runtime_issue_counts``
    and RENDERS it -- not just an events-table row nobody reads."""
    from bhiksha.ops.daily_report import build_daily_report, render_daily_report_markdown

    class RejectedStatusOrderManager(StubOrderManager):
        async def get_order_status(self, order_id):
            return "REJECTED", {"status": "REJECTED"}, None

    db_path = str(tmp_path / "events.db")
    trade_repo = SQLiteTradeStateRepository(db_path)
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(RejectedStatusOrderManager()),
        event_repository=SQLiteEventRepository(db_path),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="ABANDON_1")

    asyncio.run(supervisor._enrich_pending_partial_fills())

    with sqlite3.connect(db_path) as conn:
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events").fetchall()]
        runtime_issue_payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM events WHERE event_type = 'runtime_issue'"
            ).fetchall()
        ]
        # Pin the events to the report's trading day so the day-scoped loader picks them up.
        conn.execute("UPDATE events SET created_at = '2026-07-09T14:00:00+00:00'")
        conn.commit()

    assert "partial_fill_enrich_abandoned" in event_types
    assert len(runtime_issue_payloads) == 1
    assert runtime_issue_payloads[0]["category"] == "partial_fill_abandoned"
    assert runtime_issue_payloads[0]["error"] == "terminal_status:REJECTED"
    assert runtime_issue_payloads[0]["stage"] == "partial_fill_enrichment_sweep"

    report = build_daily_report(db_path, trading_date="2026-07-09")
    assert report["provider_health"]["runtime_issue_counts"] == {"partial_fill_abandoned": 1}

    markdown = render_daily_report_markdown(report)
    assert "## Runtime Issues" in markdown
    assert "`partial_fill_abandoned`: `1`" in markdown


# --------------------------------------------------------------------------- #
# Adversarial-audit regressions on the item-#21/#22 slice (2026-07-09). These
# are the auditor's confirmed repros (r4a/r4b/r7/r5 + findings 3 and 4) with
# INVERTED assertions: they assert the CORRECT behavior after the fixes.
#
# Root cause of r4a/r4b/r7 (audit finding 1): reconciliation
# (_refresh_reconciliation, ~15s cadence and always at startup) replaces
# tracker positions with the broker's LIVE quantity, which already excludes a
# partially-filled lot. A guard comparing filledQuantity against
# position.quantity therefore double-counts the fill. The fix keys both the
# full-fill check and the residual on the dead order's OWN placed quantity
# (payload["quantity"]), which the real broker order object carries.
# --------------------------------------------------------------------------- #

_RECON_OPTION = "AMD260713P00500000"


class ReconciledDeadStatusOrderManager(StubOrderManager):
    """Dead exit order DEAD_1 (CANCELED) whose payload carries the order's
    OWN placed quantity alongside filledQuantity, as the real broker order
    object does. Close orders auto-number CLOSE_1, CLOSE_2, ..."""

    def __init__(self, *, placed_quantity: int, filled_quantity, include_fill_truth: bool = True):
        self.close_orders: list[tuple[str, int]] = []
        self._next = 0
        self.dead_payload = {
            "orderId": "DEAD_1",
            "instrument": {"symbol": _RECON_OPTION, "type": "OPTION"},
            "status": "CANCELED",
            "side": "SELL",
            "type": "LIMIT",
            "openCloseIndicator": "CLOSE",
            "quantity": str(placed_quantity),
            "filledQuantity": filled_quantity,
        }
        if include_fill_truth:
            self.dead_payload["averagePrice"] = "13.10"
            self.dead_payload["closedAt"] = "2026-07-09T14:05:00Z"

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del option_symbol, exit_mode, limit_price
        self._next += 1
        order_id = f"CLOSE_{self._next}"
        self.close_orders.append((order_id, int(quantity)))
        return OrderResult(order_id=order_id)

    async def get_option_quote(self, option_symbol):
        return PublicQuote(symbol=option_symbol, bid=13.05, ask=13.10, last=13.07, open_interest=200, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        if order_id == "DEAD_1":
            return "CANCELED", dict(self.dead_payload), None
        return "NEW", {"status": "NEW"}, None

    async def cancel_order(self, order_id):
        del order_id
        return True, None


def test_exit_reprice_blocks_after_cancel_ack_while_order_is_pending_cancel(tmp_path) -> None:
    class PendingCancelOrderManager(ReconciledDeadStatusOrderManager):
        async def get_order_status(self, order_id):
            del order_id
            return (
                "PENDING_CANCEL",
                {
                    "orderId": "DEAD_1",
                    "status": "PENDING_CANCEL",
                    "quantity": "1",
                    "filledQuantity": None,
                },
                None,
            )

    manager = PendingCancelOrderManager(placed_quantity=1, filled_quantity=None)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=1)

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    assert plans == []
    assert manager.close_orders == []
    assert supervisor.planner.position_tracker.active_positions()[0].exit_order_id == "DEAD_1"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        blocked = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM events WHERE event_type = 'exit_reprice_blocked'"
            ).fetchall()
        ]
    assert len(blocked) == 1
    assert blocked[0]["status"] == "PENDING_CANCEL"


def _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, *, quantity, trade_id="TRADE_RECON"):
    """Seed an exit_pending trade + tracked position whose resting exit order
    is DEAD_1 (mirrors the auditor's harness)."""
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol="AMD",
                option_symbol=_RECON_OPTION,
                quantity=quantity,
                entry_price=10.40,
                entry_timestamp=datetime(2026, 7, 9, 13, 45, tzinfo=UTC),
                status="exit_pending",
                entry_order_id="ENTRY_RECON",
                exit_order_id="DEAD_1",
                exit_submitted_at=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
                exit_mode=ExitMode.STRATEGY,
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "AMD",
        deployment.deployment_id,
        trade_id=trade_id,
        option_symbol=_RECON_OPTION,
        quantity=quantity,
        entry_price=10.40,
        source="live_open",
        exit_order_id="DEAD_1",
        exit_submitted_at=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
        exit_mode=ExitMode.STRATEGY,
    )


def _reconciliation_shrunk_position(deployment, trade_repo, *, broker_quantity):
    """Build the tracker position exactly as runtime._refresh_reconciliation
    does after a restart or periodic pass: the broker's LIVE quantity (which
    already excludes any filled lot), carrying exit state from the
    exit_pending trade record."""
    from bhiksha.state.reconciliation import reconcile_public_positions

    trades = asyncio.run(trade_repo.get_recent_trades(limit=200))
    positions = reconcile_public_positions(
        [{
            "instrument": {"symbol": _RECON_OPTION, "type": "OPTION"},
            "quantity": str(broker_quantity),
            "openedAt": "2026-07-09T13:45:00Z",
        }],
        [deployment],
        orders=[],  # DEAD_1 is CANCELED at the broker -> not in open orders
        known_trades=trades,
    )
    assert len(positions) == 1
    return positions[0]


def test_exit_dead_status_after_reconciliation_shrunk_quantity_resubmits_residual_not_finalize(tmp_path) -> None:
    """Inverted r4a (audit finding 1): 2-lot exit order DEAD_1 dies with 1
    filled; reconciliation has already replaced the tracker position with the
    broker's live quantity = 1. Pre-fix the ladder read filled(1) >=
    position.quantity(1) and spuriously FINALIZED -- closed trade, dropped
    position, one live unprotected contract with no resting exit. Post-fix the
    ladder keys on the order's placed quantity (2): partial fill, residual 1
    resubmitted, trade stays exit_pending."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity="1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    position = _reconciliation_shrunk_position(deployment, trade_repo, broker_quantity=1)
    assert position.quantity == 1 and position.exit_order_id == "DEAD_1"
    supervisor.planner.position_tracker.replace_positions([position])

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # Inverted: a residual close order IS placed (pre-fix: none, spurious close).
    assert om.close_orders == [("CLOSE_1", 1)]
    assert len(plans) == 1 and plans[0].quantity == 1
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 1 and tracked.exit_order_id == "CLOSE_1"

    with sqlite3.connect(tmp_path / "events.db") as conn:
        trade_row = conn.execute(
            "SELECT status, quantity, exit_order_id FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()
        legs = conn.execute(
            "SELECT order_id, closed_quantity, origin FROM trade_partial_fills WHERE trade_id = 'TRADE_RECON'"
        ).fetchall()
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events").fetchall()]
    # Inverted: trade is NOT closed; the filled leg is banked once.
    assert trade_row == ("exit_pending", 1, "CLOSE_1")
    assert legs == [("DEAD_1", 1, "exit_dead_status")]
    assert "exit_dead_status_filled" not in event_types
    assert "exit_dead_status_partial_fill" in event_types


def test_exit_dead_status_after_reconciliation_shrunk_three_lot_covers_full_residual(tmp_path) -> None:
    """Inverted r4b (audit finding 1, N=3): 3-lot order dies with 1 filled;
    reconciliation already shrank the tracker to the broker's live 2. Pre-fix
    residual = 2 - 1 = 1 (undersell: one live contract left with no exit and
    no stop). Post-fix residual = placed(3) - filled(1) = 2: full coverage."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=3, filled_quantity="1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=3)

    position = _reconciliation_shrunk_position(deployment, trade_repo, broker_quantity=2)
    assert position.quantity == 2 and position.exit_order_id == "DEAD_1"
    supervisor.planner.position_tracker.replace_positions([position])

    asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # Inverted: the replacement exit covers BOTH remaining contracts.
    assert om.close_orders == [("CLOSE_1", 2)]
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 2 and tracked.exit_order_id == "CLOSE_1"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        legs = conn.execute(
            "SELECT order_id, closed_quantity, origin FROM trade_partial_fills WHERE trade_id = 'TRADE_RECON'"
        ).fetchall()
    # Leg bookkeeping accounts for all 3 lots: 1 banked + 2 covered.
    assert legs == [("DEAD_1", 1, "exit_dead_status")]


def test_exit_dead_status_residual_stays_managed_after_readoption_no_zombie(tmp_path) -> None:
    """Inverted r7 (aftermath of r4a): with the finding-1 fix the trade is
    never spuriously closed, so the next reconciliation pass re-adopts the
    still-live lot as a fully MANAGED position -- live exit order id, exit_mode
    set, still exit_pending -- instead of the pre-fix zombie (closed trade
    flipped back to exit_pending carrying the dead order id with
    exit_mode=None, unmanageable forever)."""
    from bhiksha.state.reconciliation import reconcile_public_positions

    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity="1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    position = _reconciliation_shrunk_position(deployment, trade_repo, broker_quantity=1)
    supervisor.planner.position_tracker.replace_positions([position])
    asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    assert om.close_orders == [("CLOSE_1", 1)]  # residual resubmitted, no finalize

    # Next reconciliation pass (~15s later): broker still reports the lot.
    trades = asyncio.run(trade_repo.get_recent_trades(limit=200))
    readopted = reconcile_public_positions(
        [{
            "instrument": {"symbol": _RECON_OPTION, "type": "OPTION"},
            "quantity": "1",
            "openedAt": "2026-07-09T13:45:00Z",
        }],
        [deployment],
        orders=[],
        known_trades=trades,
    )
    assert len(readopted) == 1
    live = readopted[0]
    # Inverted zombie checks: LIVE order id (not the dead one), exit_mode set.
    assert live.exit_order_id == "CLOSE_1"
    assert live.exit_mode is not None
    supervisor.planner.position_tracker.replace_positions([live])

    # sync_lifecycle keeps the genuinely-pending trade exit_pending -- it never
    # flips a closed trade back open, because the trade was never closed.
    asyncio.run(supervisor.sync_lifecycle())
    with sqlite3.connect(tmp_path / "events.db") as conn:
        status = conn.execute(
            "SELECT status FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()[0]
    assert status == "exit_pending"

    # And the pending-exit loop still MANAGES the position (pre-fix zombie:
    # exit_mode None dead-ended before any poll). The tick polls the live
    # order and the residual lot keeps a resting exit order.
    asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    with sqlite3.connect(tmp_path / "events.db") as conn:
        polled = [
            json.loads(row[0])["exit_order_id"]
            for row in conn.execute("SELECT payload FROM events WHERE event_type = 'exit_pending_status'").fetchall()
        ]
    assert "CLOSE_1" in polled
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 1 and tracked.exit_order_id is not None and tracked.exit_mode is not None


def test_exit_reprice_cancel_race_uses_order_quantity_not_reconciled_position_quantity(tmp_path) -> None:
    """Audit finding 1 applied to the merged reprice ladder
    (_resolve_exit_cancel_for_reprice): same latent assumption, narrower race
    window. A reconciliation-shrunk 1-lot position whose canceled 2-lot order
    reports filledQuantity=1 must resolve as a PARTIAL with residual 1 (order
    quantity 2 - filled 1), not a spurious full-fill finalize (pre-fix:
    filled 1 >= position.quantity 1)."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity="1")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    position = _reconciliation_shrunk_position(deployment, trade_repo, broker_quantity=1)
    assert position.quantity == 1
    supervisor.planner.position_tracker.replace_positions([position])

    outcome = asyncio.run(
        supervisor._resolve_exit_cancel_for_reprice(
            deployment,
            position,
            exit_order_id="DEAD_1",
            reason="exit_reprice",
            now=datetime(2026, 7, 9, 14, 6, tzinfo=UTC),
        )
    )

    assert outcome.action == "resubmit"
    assert outcome.position is not None and outcome.position.quantity == 1
    with sqlite3.connect(tmp_path / "events.db") as conn:
        legs = conn.execute(
            "SELECT order_id, closed_quantity, origin FROM trade_partial_fills WHERE trade_id = 'TRADE_RECON'"
        ).fetchall()
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events").fetchall()]
    assert legs == [("DEAD_1", 1, "exit_cancel_race")]
    assert "exit_reprice_cancel_race_filled" not in event_types
    assert "exit_cancel_race_partial_fill" in event_types


def test_dead_full_fill_exit_truth_reenriched_once_average_price_available(tmp_path) -> None:
    """Inverted r5 (audit finding 2): a dead order that fully filled but whose
    payload had no averagePrice yet finalizes with exit_price NULL (the truth
    genuinely was not published). The closed-truth retry
    (_enrich_recent_closed_exit_truth) must now accept the dead-with-fill
    payload once the broker publishes the price -- pre-fix it only accepted
    status==FILLED, so the NULL was permanent."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity="2", include_fill_truth=False)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT status, exit_order_id, exit_price, exit_filled_quantity FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()
    assert row == ("closed", "DEAD_1", None, 2)  # finalized; price not yet published

    # Broker publishes the fill economics on a later readback.
    om.dead_payload["averagePrice"] = "13.10"
    om.dead_payload["closedAt"] = "2026-07-09T14:05:00Z"

    trades = asyncio.run(trade_repo.get_recent_trades(limit=10))
    asyncio.run(supervisor._enrich_recent_closed_exit_truth(trades))

    with sqlite3.connect(tmp_path / "events.db") as conn:
        row = conn.execute(
            "SELECT status, exit_order_id, exit_price, exit_filled_quantity, exit_filled_at FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()
        event_types = [r[0] for r in conn.execute("SELECT event_type FROM events").fetchall()]
    # Inverted: the retry repaired the truth from the CANCELED-but-filled payload.
    assert row == ("closed", "DEAD_1", 13.10, 2, "2026-07-09T14:05:00+00:00")
    assert "exit_fill_enriched" in event_types


def test_exit_dead_status_unparseable_filled_quantity_blocks_resubmit_and_escalates(tmp_path) -> None:
    """Audit finding 3: a PRESENT but unparseable filledQuantity ("N/A") on a
    dead exit order must NOT be treated as confirmed-unfilled (full resubmit =
    potential oversell). The poll is skipped fail-closed, a runtime_issue
    (category exit_fill_unparseable) is emitted, and the position keeps its
    exit_order_id so the next poll re-reads the order."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity="N/A")
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # Fail closed: nothing resubmitted, nothing recorded, order left for re-read.
    assert plans == []
    assert om.close_orders == []
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 2 and tracked.exit_order_id == "DEAD_1"
    with sqlite3.connect(tmp_path / "events.db") as conn:
        issues = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM events WHERE event_type = 'runtime_issue'").fetchall()
        ]
        legs = conn.execute("SELECT id FROM trade_partial_fills").fetchall()
        status = conn.execute(
            "SELECT status FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()[0]
    assert legs == [] and status == "exit_pending"
    assert len(issues) == 1
    assert issues[0]["category"] == "exit_fill_unparseable"
    assert issues[0]["order_id"] == "DEAD_1"
    assert "N/A" in issues[0]["error"]
    assert issues[0]["stage"] == "exit_dead_status_resubmit"

    # The next poll retries (still blocked while the payload stays garbage).
    asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    assert om.close_orders == []
    assert supervisor.planner.position_tracker.active_positions()[0].exit_order_id == "DEAD_1"


def test_exit_dead_status_null_filled_quantity_is_zero_fill_full_resubmit_not_blocked(tmp_path) -> None:
    """Inverted round-2 f3 (audit round 2, HIGH): JSON null is Public's
    STANDARD zero-fill idiom on order objects -- empirically every real
    zero-fill order reads "filledQuantity": null (never key-absent, never
    "0"). A routine dead-UNFILLED exit order must therefore full-resubmit on
    the first poll exactly as pre-#21, NOT wedge in the finding-3 blocked
    path with a runtime_issue per 2s poll forever. Only genuinely garbled
    NON-NULL values ("N/A", "") block."""
    om = ReconciledDeadStatusOrderManager(placed_quantity=2, filled_quantity=None)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    _seed_exit_pending_dead_order(supervisor, trade_repo, deployment, quantity=2)

    for _ in range(3):
        asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))

    # Inverted: full-quantity resubmit happened on the FIRST poll (no wedge),
    # and the subsequent polls just watch the replacement order.
    assert om.close_orders == [("CLOSE_1", 2)]
    tracked = supervisor.planner.position_tracker.active_positions()[0]
    assert tracked.quantity == 2 and tracked.exit_order_id == "CLOSE_1"

    with sqlite3.connect(tmp_path / "events.db") as conn:
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'runtime_issue'"
        ).fetchone()[0]
        legs = conn.execute("SELECT id FROM trade_partial_fills").fetchall()
        status = conn.execute(
            "SELECT status FROM trade_sessions WHERE trade_id = 'TRADE_RECON'"
        ).fetchone()[0]
    # Inverted: no alert flood (zero-fill is not an anomaly), no phantom leg,
    # trade still legitimately exit_pending on the replacement order.
    assert issue_count == 0
    assert legs == []
    assert status == "exit_pending"


def test_abandonment_escalation_survives_crash_at_durable_mark(tmp_path) -> None:
    """Audit finding 4: the runtime_issue escalation must be appended BEFORE
    the durable abandoned-mark. If the mark lands first and the process dies
    before the events, the row is excluded from every future sweep and the
    escalation is lost forever. Post-fix a crash at the mark leaves the row
    pending: the next sweep redoes the abandonment (duplicate escalation
    beats a lost one)."""

    class CrashingMarkRepo(SQLiteTradeStateRepository):
        crash = True

        async def mark_partial_fill_abandoned(self, record_id, *, reason):
            if self.crash:
                raise RuntimeError("simulated crash at durable abandon mark")
            await super().mark_partial_fill_abandoned(record_id, reason=reason)

    class RejectedStatusOrderManager(StubOrderManager):
        async def get_order_status(self, order_id):
            return "REJECTED", {"status": "REJECTED"}, None

    trade_repo = CrashingMarkRepo(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(RejectedStatusOrderManager()),
        event_repository=SQLiteEventRepository(str(tmp_path / "events.db")),
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    _seed_partial_fill_record(trade_repo, order_id="CRASH_1")

    with pytest.raises(RuntimeError):
        asyncio.run(supervisor._enrich_pending_partial_fills())

    with sqlite3.connect(tmp_path / "events.db") as conn:
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'runtime_issue'"
        ).fetchone()[0]
        abandoned = conn.execute(
            "SELECT abandoned_reason FROM trade_partial_fills WHERE order_id = 'CRASH_1'"
        ).fetchone()[0]
    # Escalation recorded even though the durable mark crashed; row NOT
    # excluded, so the next sweep retries the abandonment.
    assert issue_count == 1
    assert abandoned is None

    trade_repo.crash = False
    asyncio.run(supervisor._enrich_pending_partial_fills())
    with sqlite3.connect(tmp_path / "events.db") as conn:
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'runtime_issue'"
        ).fetchone()[0]
        abandoned = conn.execute(
            "SELECT abandoned_reason FROM trade_partial_fills WHERE order_id = 'CRASH_1'"
        ).fetchone()[0]
    assert issue_count == 2  # the tolerated duplicate
    assert abandoned == "terminal_status:REJECTED"


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


def test_execution_supervisor_enriches_fast_filled_pending_exit_after_position_disappears(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    repo = SQLiteEventRepository(str(db_path))
    trade_repo = SQLiteTradeStateRepository(str(db_path))
    order_manager = StatusMapOrderManager(
        {
            "EXIT-NVDA-RACE": (
                "FILLED",
                {
                    "orderId": "EXIT-NVDA-RACE",
                    "instrument": {"symbol": "NVDA260722P00200000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "8",
                    "averagePrice": "2.07",
                    "closedAt": "2026-07-13T14:38:06Z",
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
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="NVDA-RACE",
                deployment_id="nvda_live",
                symbol="NVDA",
                option_symbol="NVDA260722P00200000",
                quantity=8,
                entry_price=2.18,
                entry_timestamp=datetime(2026, 7, 13, 13, 52, tzinfo=UTC),
                status="exit_pending",
                entry_order_id="ENTRY-NVDA-RACE",
                exit_order_id="EXIT-NVDA-RACE",
                exit_limit_price=2.07,
                exit_submitted_at=datetime(2026, 7, 13, 14, 38, 5, tzinfo=UTC),
                exit_mode=ExitMode.STRATEGY,
                exit_rule="no_progress",
            )
        )
    )

    # The broker position is already absent; only durable exit identity can
    # recover the terminal fill instead of recording an unknown close.
    asyncio.run(supervisor.sync_lifecycle())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, exit_order_id, exit_price, exit_filled_quantity,
                   exit_filled_at, exit_order_status, exit_rule
            FROM trade_sessions
            WHERE trade_id = 'NVDA-RACE'
            """
        ).fetchone()

    assert row == (
        "closed",
        "EXIT-NVDA-RACE",
        2.07,
        8,
        "2026-07-13T14:38:06+00:00",
        "FILLED",
        "no_progress",
    )


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


def test_public_virtual_target_waits_for_confirmed_stop_cancel(tmp_path) -> None:
    class ConfirmRequiredOrderManager(RecordingOrderManager):
        allows_exit_submission_before_cancel_confirmation = False

    order_manager = ConfirmRequiredOrderManager(quote_bid=3.30, cancel_success=False)
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
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
        source="live_open",
        stop_order_id="STOP123",
        stop_price=1.1,
        target_price=3.35,
    )

    managed = asyncio.run(
        supervisor.manage_open_position(
            deployment,
            supervisor.planner.position_tracker.active_positions()[0],
            dry_run=False,
        )
    )

    assert managed is not None
    assert managed.stop_order_id == "STOP123"
    assert managed.target_order_id is None
    assert ("cancel", "STOP123") in order_manager.calls
    assert ("place_target", 3.35) not in order_manager.calls


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


class _FailingPartialCloseOrderManager(_QtyRecordingOrderManager):
    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        self.close_calls.append((option_symbol, int(quantity), exit_mode.value))
        return OrderResult(order_id=None, error="broker rejected partial close")


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


def test_partial_scale_live_submit_failure_does_not_bank_or_reduce_position(tmp_path) -> None:
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    om = _FailingPartialCloseOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="T_FAIL_PARTIAL",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                quantity=4,
                entry_price=2.0,
                status="open_protected",
                entry_order_id="ENTRY_FULL",
                stop_order_id="STOP_FULL",
                stop_price=1.5,
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T_FAIL_PARTIAL",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=2.0,
        source="live_open",
        order_id="ENTRY_FULL",
        stop_order_id="STOP_FULL",
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = _partial_scale_decision(deployment.deployment_id, exit_quantity=3)

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is not None
    assert plan.order_id is None
    assert plan.error == "broker rejected partial close"
    assert om.close_calls == [("QQQ260401P00556000", 3, ExitMode.STRATEGY.value)]
    # The bank did not happen, so protection is restored on the original full
    # 4-lot position -- not a residual 1-lot.
    assert om.stop_calls == [("QQQ260401P00556000", 1.5, 4)]
    survivor = supervisor.planner.position_tracker.active_positions()[0]
    assert survivor.quantity == 4
    assert survivor.stop_order_id == "STOP_RESIDUAL"
    assert survivor.exit_mode is None and survivor.exit_order_id is None

    with sqlite3.connect(tmp_path / "events.db") as conn:
        trade_row = conn.execute(
            """
            SELECT status, quantity, stop_order_id, stop_price
            FROM trade_sessions
            WHERE trade_id = 'T_FAIL_PARTIAL'
            """
        ).fetchone()
        partial_count = conn.execute(
            "SELECT COUNT(*) FROM trade_partial_fills WHERE trade_id = 'T_FAIL_PARTIAL'"
        ).fetchone()[0]
        event_types = [row[0] for row in conn.execute("SELECT event_type FROM events ORDER BY id").fetchall()]

    assert trade_row == ("open_protected", 4, "STOP_RESIDUAL", 1.5)
    assert partial_count == 0
    assert "exit_submission_failure" in event_types
    assert "partial_scale_submission" not in event_types


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


class _QqqPartialLegFixtureOrderManager(StubOrderManager):
    """ITEM B fixture (2026-07-08 hygiene batch): a genuine 2-lot partial bank
    (T1 banks 1 of 2, unlike item A's 1-lot full-bank case) followed by the
    runner's own full exit, mirroring the real QQQ same-day
    ``high_water_giveback`` example the operator cited (exit 3.75).
    """

    def __init__(self) -> None:
        self.close_calls: list[tuple[str, str, int]] = []
        self._next_id = 0

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        del exit_mode, limit_price
        self._next_id += 1
        order_id = f"QQQ_CLOSE_{self._next_id}"
        self.close_calls.append((order_id, option_symbol, int(quantity)))

        class Result:
            pass

        result = Result()
        result.order_id = order_id
        result.error = None
        return result

    async def place_stop_loss_order(self, option_symbol, stop_price, quantity, *, order_id=None):
        del option_symbol, stop_price, quantity, order_id

        class Result:
            pass

        result = Result()
        result.order_id = "STOP_RESIDUAL_QQQ"
        result.error = None
        return result

    async def cancel_order(self, order_id):
        del order_id
        return True, None

    async def get_option_quote(self, option_symbol):
        return PublicQuote(symbol=option_symbol, bid=3.75, ask=3.80, last=3.78, open_interest=500, outcome="SUCCESS")

    async def get_order_status(self, order_id):
        if order_id == "QQQ_CLOSE_1":
            # The banked T1 partial leg (1 of 2 contracts).
            return (
                "FILLED",
                {
                    "orderId": "QQQ_CLOSE_1",
                    "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "3.10",
                    "closedAt": "2026-07-08T14:10:00Z",
                },
                None,
            )
        if order_id == "QQQ_CLOSE_2":
            # The runner's own high_water_giveback full exit (matches the
            # operator's cited real QQQ fill of 3.75).
            return (
                "FILLED",
                {
                    "orderId": "QQQ_CLOSE_2",
                    "instrument": {"symbol": "QQQ260401P00556000", "type": "OPTION"},
                    "status": "FILLED",
                    "side": "SELL",
                    "type": "LIMIT",
                    "openCloseIndicator": "CLOSE",
                    "filledQuantity": "1",
                    "averagePrice": "3.75",
                    "closedAt": "2026-07-08T14:45:00Z",
                },
                None,
            )
        return None, None, "unknown_order"


def test_partial_leg_pnl_fixture_qqq_t1_bank_then_runner_exit(tmp_path) -> None:
    """ITEM B (2026-07-08 hygiene batch): audit + fixture repro for whether a
    banked partial leg's own economics survive durably.

    Before this fix: trade_sessions held a SINGLE row per trade_id, and
    ``_handle_partial_scale_locked`` overwrote ``quantity`` to the residual on
    every bank -- the banked leg's fill price/quantity/timestamp existed
    NOWHERE durable (only an unconfirmed order_id in the append-only
    ``partial_scale_submission`` event). Weekly per-leg P&L reconstruction
    could not recover the banked leg's contribution or even the position's
    original size.

    After this fix: ``trade_partial_fills`` holds one durably-enriched row per
    banked leg; ``trade_sessions`` still holds the final (residual) state as
    before. Together: original size = trade_sessions.quantity (final
    residual, 1) + sum(trade_partial_fills.closed_quantity) (1) = 2.
    """
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    order_manager = _QqqPartialLegFixtureOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(order_manager),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    asyncio.run(
        trade_repo.upsert_trade(
            TradeRecord(
                trade_id="TRADE_QQQ_PARTIAL",
                deployment_id=deployment.deployment_id,
                symbol="QQQ",
                option_symbol="QQQ260401P00556000",
                quantity=2,
                entry_price=2.50,
                entry_timestamp=datetime(2026, 7, 8, 13, 30, tzinfo=UTC),
                status="open_protected",
                entry_order_id="ENTRY_QQQ",
                stop_order_id="STOP_FULL_QQQ",
                stop_price=1.50,
            )
        )
    )
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE_QQQ_PARTIAL",
        option_symbol="QQQ260401P00556000",
        quantity=2,
        entry_price=2.50,
        source="live_open",
        stop_order_id="STOP_FULL_QQQ",
        stop_price=1.50,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    # Mirrors profile_decision_to_exit_decision's real output for a genuine
    # (bank_qty < quantity) T1 partial: PARTIAL_SCALE fsm_action -> the
    # "partial_scale" feature IS set, unlike item A's 1-lot full-bank case.
    partial_decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 7, 8, 14, 10, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_target_1_partial"],
        cancel_protection_orders=True,
        features={
            "profile_id": "market_impulse_qqq",
            "profile_rule": "target_1_partial",
            "profile_fsm_action": "partial_scale",
            "exit_quantity": 1,
            "partial_scale": True,
            "bank_quantity": 1,
            "remaining_quantity": 1,
        },
    )

    partial_plan = asyncio.run(supervisor.handle_exit(deployment, position, partial_decision, dry_run=False))
    assert partial_plan is not None and partial_plan.quantity == 1
    assert order_manager.close_calls == [("QQQ_CLOSE_1", "QQQ260401P00556000", 1)]

    # sync_lifecycle's new _enrich_pending_partial_fills sweep backfills the
    # banked leg's confirmed fill truth (mirrors _enrich_recent_closed_exit_truth).
    asyncio.run(supervisor.sync_lifecycle())

    residual = supervisor.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1
    assert residual.exit_mode is None and residual.exit_order_id is None

    # Runner's own full exit (high_water_giveback -- NOT a partial).
    runner_decision = ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 7, 8, 14, 45, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_high_water_giveback:PATIENT"],
        cancel_protection_orders=True,
        features={"profile_id": "market_impulse_qqq", "profile_rule": "high_water_giveback"},
    )
    asyncio.run(supervisor.handle_exit(deployment, residual, runner_decision, dry_run=False))
    plans = asyncio.run(supervisor.manage_pending_exits({deployment.deployment_id: deployment}))
    assert len(plans) == 1

    with sqlite3.connect(tmp_path / "events.db") as conn:
        trade_row = conn.execute(
            """
            SELECT status, quantity, entry_price, exit_order_id, exit_price, exit_filled_quantity,
                   exit_filled_at, exit_rule
            FROM trade_sessions
            WHERE trade_id = 'TRADE_QQQ_PARTIAL'
            """
        ).fetchone()
        partial_rows = conn.execute(
            """
            SELECT trade_id, closed_quantity, order_id, exit_rule, fill_price, fill_quantity, filled_at, order_status
            FROM trade_partial_fills
            WHERE trade_id = 'TRADE_QQQ_PARTIAL'
            ORDER BY id ASC
            """
        ).fetchall()

    # trade_sessions holds only the FINAL (residual) state -- the runner's own
    # leg. quantity=1 (the residual, NOT the original 2); exit_price=3.75 is
    # the runner's fill only.
    assert trade_row == (
        "closed",
        1,
        2.50,
        "QQQ_CLOSE_2",
        3.75,
        1,
        "2026-07-08T14:45:00+00:00",
        "high_water_giveback",
    )
    # trade_partial_fills holds the banked leg's own durable economics --
    # previously recorded nowhere.
    assert partial_rows == [
        (
            "TRADE_QQQ_PARTIAL",
            1,
            "QQQ_CLOSE_1",
            "target_1_partial",
            3.10,
            1,
            "2026-07-08T14:10:00+00:00",
            "FILLED",
        )
    ]
    # Original position size is recoverable ONLY by combining both tables.
    original_quantity = trade_row[1] + sum(row[1] for row in partial_rows)
    assert original_quantity == 2


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


# =========================================================================== #
# Adversarial re-audit fixes: NEW-1..NEW-6 (residual protection, naked windows,
# state leak, runtime-mode enum). Each test proves one fix.
# =========================================================================== #


def _profile_partial_decision(deployment_id, *, exit_quantity, entry_premium, risk_per_contract,
                              cancel_protection_orders=True, symbol="QQQ"):
    """A profile target-1 partial carrying the evaluator's diagnostics.

    The real evaluator stamps ``entry_premium`` and ``risk_per_contract`` into
    ``features`` (via ``_diag``); the supervisor derives the residual stop from
    them when no prior resting stop exists (NEW-1).
    """
    return ExitDecision(
        deployment_id=deployment_id,
        symbol=symbol,
        timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        exit=True,
        action="square_off",
        reason=["profile_target_1_partial"],
        cancel_protection_orders=cancel_protection_orders,
        features={
            "exit_quantity": exit_quantity,
            "partial_scale": True,
            "profile_id": "FLASH_REVERSAL",
            "entry_premium": entry_premium,
            "risk_per_contract": risk_per_contract,
        },
    )


def test_new1_partial_on_stopless_position_leaves_residual_protected(tmp_path) -> None:
    # NEW-1: a profile partial on a position with NO prior resting stop
    # (stop_price=None, stop_order_id=None) must still leave the residual
    # PROTECTED — a residual stop derived from the profile (entry - risk).
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
        # NO stop in place at all:
        stop_order_id=None,
        stop_price=None,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    # entry 2.0, risk 0.5 -> derived residual stop = 1.5
    decision = _profile_partial_decision(
        deployment.deployment_id, exit_quantity=3, entry_premium=2.0, risk_per_contract=0.5
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is not None and plan.quantity == 3
    residual = supervisor.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1
    # The residual is NOT naked: a stop was placed and recorded.
    assert residual.stop_order_id == "STOP_RESIDUAL"
    assert residual.stop_price == 1.5  # derived entry(2.0) - risk(0.5)
    assert om.stop_calls == [("QQQ260401P00556000", 1.5, 1)]


def test_new2_partial_no_cancel_with_live_stop_avoids_double_stop(tmp_path) -> None:
    # NEW-2: cancel_protection_orders=False but a live full-size stop exists. The
    # handler must cancel-then-replace (drop the stale full-size stop) so the
    # residual ends with EXACTLY ONE stop, never two.
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
        stop_order_id="STOP_FULL",  # live full-size stop
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = _profile_partial_decision(
        deployment.deployment_id,
        exit_quantity=3,
        entry_premium=2.0,
        risk_per_contract=0.5,
        cancel_protection_orders=False,  # decision did NOT request a cancel
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    assert plan is not None and plan.quantity == 3
    # The stale full-size stop was cancelled before the residual stop was placed.
    assert "STOP_FULL" in om.cancel_calls
    # EXACTLY ONE residual stop placed (no double stop on the residual).
    assert om.stop_calls == [("QQQ260401P00556000", 1.5, 1)]
    residual = supervisor.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1
    assert residual.stop_order_id == "STOP_RESIDUAL"


class _StopPlaceFailingOrderManager(StubOrderManager):
    """Cancels OK; every place_stop_loss_order FAILS (order_id=None)."""

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.stop_calls: list[tuple[str, float, int]] = []

    async def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return True, None  # cancel succeeds

    async def place_stop_loss_order(self, option_symbol, stop_price, quantity, *, order_id=None):
        self.stop_calls.append((option_symbol, round(stop_price, 2), int(quantity)))
        return OrderResult(order_id=None, error="stop_rejected")  # place FAILS

    async def place_close_order(self, option_symbol, quantity, *, exit_mode, limit_price=None):
        return OrderResult(order_id="CLOSE_OK")


def _failing_stop_supervisor(tmp_path):
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    om = _StopPlaceFailingOrderManager()
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(om),
        event_repository=repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    return supervisor, om, repo


def _events_of_type(repo, event_type):
    with sqlite3.connect(repo.db_path) as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE event_type = ? ORDER BY id", (event_type,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_new3_replacement_stop_cancel_ok_place_fail_marks_for_reprotection(tmp_path) -> None:
    # NEW-3: STOP_TO_BREAKEVEN where cancel succeeds but the new stop placement
    # fails (and the retry also fails). The position must NOT be left silently
    # naked: it is marked for reprotection (stop_order_id=None so the monitor
    # re-arms next tick) and the unprotected state is recorded.
    supervisor, om, repo = _failing_stop_supervisor(tmp_path)
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
        replacement_stop_price=2.0,
    )

    asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    # Old stop was cancelled; placement attempted twice (initial + one retry).
    assert "STOP_OLD" in om.cancel_calls
    assert len(om.stop_calls) == 2  # retry-once on place-fail
    updated = supervisor.planner.position_tracker.active_positions()[0]
    # Marked for reprotection: no stale stop id, and (NEW-6) no stale stop price.
    assert updated.stop_order_id is None
    assert updated.stop_price is None
    # The monitor re-arms exactly this condition (no stop, no target).
    assert updated.target_order_id is None
    # Unprotected state recorded, not just ridden.
    issues = _events_of_type(repo, "runtime_issue")
    assert any(
        i.get("category") == "protective_stop_failure" and i.get("stage") == "profile_replacement_stop"
        for i in issues
    )
    repl = _events_of_type(repo, "profile_replacement_stop")
    assert repl and repl[-1]["unprotected"] is True
    assert repl[-1]["new_stop_price"] is None  # NEW-6: not the failed price


def test_new6_replacement_stop_does_not_persist_price_on_failure(tmp_path) -> None:
    # NEW-6 (focused): a failed replacement stop must leave stop_price=None so a
    # downstream `stop_price is not None` protected-check is not fooled.
    supervisor, om, _ = _failing_stop_supervisor(tmp_path)
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
        replacement_stop_price=2.0,
    )
    updated = asyncio.run(supervisor._apply_replacement_stop(deployment, position, decision, dry_run=False))
    assert updated.stop_order_id is None
    assert updated.stop_price is None  # NOT 2.0


def test_medium1_partial_residual_stop_price_cleared_on_place_fail(tmp_path) -> None:
    # MEDIUM-1: a profile partial cancels the full-size stop, sells the banked qty,
    # then tries to re-arm a residual stop. When that placement FAILS the residual
    # must NOT keep a phantom ``stop_price`` (which would fool a downstream
    # ``stop_price is not None`` protected-check). With ``stop_order_id is None``
    # the residual ``stop_price`` must also be None so the monitor re-arms it.
    supervisor, om, repo = _failing_stop_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=4,
        entry_price=2.0,
        source="live_open",
        stop_order_id="STOP_FULL",  # live full-size stop @1.5 covering all 4
        stop_price=1.5,
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    decision = _profile_partial_decision(
        deployment.deployment_id, exit_quantity=3, entry_premium=2.0, risk_per_contract=0.5
    )

    plan = asyncio.run(supervisor.handle_exit(deployment, position, decision, dry_run=False))

    # The banked 3 were closed (CLOSE_OK), the full-size stop was cancelled, and a
    # residual stop placement was attempted (and failed).
    assert plan is not None and plan.quantity == 3
    assert "STOP_FULL" in om.cancel_calls
    assert om.stop_calls  # a residual stop placement was attempted
    residual = supervisor.planner.position_tracker.active_positions()[0]
    assert residual.quantity == 1
    # The residual is left for reprotection: NO stop order id AND NO phantom price.
    assert residual.stop_order_id is None
    assert residual.stop_price is None  # MEDIUM-1: not the stale 1.5
    # The monitor re-arms exactly this condition (no stop, no target).
    assert residual.target_order_id is None
    # The persisted trade record carries no phantom stop price either.
    records = _events_of_type(repo, "partial_scale_submission")
    assert records and records[-1]["residual_protected"] is False
    assert records[-1]["restored_stop_price"] is None


def test_new4_eod_sweep_clears_profile_exit_state(tmp_path) -> None:
    # NEW-4: the EOD hard-flat sweep (close_due_positions) is terminal -> clear.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="shadow",  # dry-run close path
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    supervisor.get_or_create_profile_exit_state(position, entry_premium=2.0)
    key = supervisor._profile_state_key(position)
    assert key in supervisor._profile_exit_states

    # now well past hard-flat -> sweep closes the position
    asyncio.run(
        supervisor.close_due_positions(
            {deployment.deployment_id: deployment},
            now=datetime(2026, 3, 30, 20, 30, tzinfo=UTC),
            dry_run=True,
        )
    )
    assert supervisor.planner.position_tracker.active_positions() == []
    assert key not in supervisor._profile_exit_states  # state cleared


def test_new4_halt_and_flatten_clears_profile_exit_state(tmp_path) -> None:
    # NEW-4: halt_and_flatten (dry-run flat) is terminal -> clear.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="shadow",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    supervisor.get_or_create_profile_exit_state(position, entry_premium=2.0)
    key = supervisor._profile_state_key(position)
    assert key in supervisor._profile_exit_states

    asyncio.run(
        supervisor.halt_and_flatten_positions(
            {deployment.deployment_id: deployment}, dry_run=True
        )
    )
    assert supervisor.planner.position_tracker.active_positions() == []
    assert key not in supervisor._profile_exit_states


def test_new4_mark_trade_closed_with_exit_truth_clears_profile_exit_state(tmp_path) -> None:
    # NEW-4: the reconciled-fill terminal close (the common chokepoint for the
    # pending-exit FILLED path) must clear the profile-exit ladder state by trade id.
    supervisor, _ = _partial_supervisor(tmp_path)
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    supervisor.get_or_create_profile_exit_state(position, entry_premium=2.0)
    key = supervisor._profile_state_key(position)  # "trade:T1"
    assert key in supervisor._profile_exit_states

    asyncio.run(
        supervisor._mark_trade_closed_with_exit_truth(
            "T1", exit_order_id="EXIT1", status="FILLED", payload={"status": "FILLED"}
        )
    )
    assert key not in supervisor._profile_exit_states  # NEW-4: cleared


def test_new4_disappeared_trade_close_clears_profile_exit_state(tmp_path) -> None:
    # NEW-4: a disappeared (broker-vanished) position close is terminal -> clear.
    repo = SQLiteEventRepository(str(tmp_path / "events.db"))
    trade_repo = SQLiteTradeStateRepository(str(tmp_path / "events.db"))
    supervisor = ExecutionSupervisor(
        planner=RecordingPlanner(ExplodingStatusOrderManager()),
        event_repository=repo,
        trade_state_repository=trade_repo,
        app_config=AppConfig(order_fill_poll_seconds=0, order_fill_timeout_seconds=1),
    )
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="T1",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_open",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    supervisor.get_or_create_profile_exit_state(position, entry_premium=2.0)
    key = supervisor._profile_state_key(position)
    assert key in supervisor._profile_exit_states

    trade = TradeRecord(
        trade_id="T1",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        status="open_protected",
        entry_order_id="ENTRY1",
    )
    asyncio.run(supervisor._mark_disappeared_trade_closed(trade))
    assert key not in supervisor._profile_exit_states  # NEW-4: cleared


def test_new4_reconcile_pending_entry_release_clears_profile_exit_state(tmp_path) -> None:
    # NEW-4: releasing a terminal reconcile-hold entry (rejected/cancelled/expired)
    # must clear the profile-exit ladder state by full identity.
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
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    supervisor.planner.position_tracker.open_position(
        "QQQ",
        deployment.deployment_id,
        trade_id="TRADE123",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        source="live_pending",
        order_id="ENTRY123",
    )
    position = supervisor.planner.position_tracker.active_positions()[0]
    supervisor.get_or_create_profile_exit_state(position, entry_premium=2.0)
    key = supervisor._profile_state_key(position)  # "trade:TRADE123"
    assert key in supervisor._profile_exit_states

    trade = TradeRecord(
        trade_id="TRADE123",
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260401P00556000",
        quantity=1,
        entry_price=2.0,
        entry_timestamp=datetime(2026, 3, 30, 14, 30, tzinfo=UTC),
        status="pending_entry_reconcile",
        entry_order_id="ENTRY123",
    )
    asyncio.run(supervisor._reconcile_pending_entry_release(trade))
    assert key not in supervisor._profile_exit_states  # NEW-4: cleared
