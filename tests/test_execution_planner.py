import asyncio
from datetime import datetime

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, SignalDecision
from bhiksha.execution.order_manager import PublicQuote, PreflightCheck
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository
from bhiksha.risk.cash_guard import CashGuard
from bhiksha.state.position_tracker import PositionTracker
from historical_config import historical_deployment


def _enabled_deployment(deployment_id: str):
    deployment = historical_deployment(deployment_id)
    return deployment.model_copy(update={"enabled": True})


class StubChainService:
    def __init__(
        self,
        *,
        symbol: str = "QQQ",
        option_symbol: str | None = None,
        contract_type: str = "PUT",
        strike: float = 250.0,
        dte: int = 0,
        delta: float = -0.31,
        bid: float | None = 3.00,
        ask: float | None = 2.90,
    ) -> None:
        self.calls = 0
        self.symbol = symbol
        self.option_symbol = option_symbol or f"{symbol}260330{contract_type[0].upper()}00558000"
        self.contract_type = contract_type
        self.strike = strike
        self.dte = dte
        self.delta = delta
        self.bid = bid
        self.ask = ask

    async def get_chain(self, symbol: str, **kwargs):
        self.calls += 1
        return [
            OptionContractSnapshot(
                option_symbol=self.option_symbol,
                underlying_symbol=self.symbol,
                contract_type=self.contract_type,
                expiration_date="2026-03-30",
                dte=self.dte,
                strike=self.strike,
                delta=self.delta,
                bid=self.bid,
                ask=self.ask,
                open_interest=500,
            )
        ]

    async def close(self):
        return None


class StubOrderManager:
    def __init__(self) -> None:
        self.place_entry_order_calls = 0

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=2.70,
            ask=2.90,
            last=2.80,
            open_interest=550,
            outcome="SUCCESS",
        )

    async def preflight_entry(self, option_symbol: str, limit_price: float, quantity: int):
        return PreflightCheck(
            payload={"limitPrice": "2.90"},
            current_increment=0.10,
            buying_power_requirement=290.04,
            estimated_cost=289.98,
        )

    async def get_portfolio(self):
        return {
            "buyingPower": {
                "cashOnlyBuyingPower": "5000.00",
            }
        }

    async def get_account_info(self):
        return {"brokerageAccountType": "CASH"}

    async def place_entry_order(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
        order_id: str | None = None,
    ):
        del option_symbol, limit_price, quantity, order_id
        self.place_entry_order_calls += 1
        class Result:
            order_id = "OID123"
            error = None
        return Result()

    async def close(self):
        return None


class QuoteErrorOrderManager(StubOrderManager):
    async def get_option_quote(self, option_symbol: str):
        raise RuntimeError(f"quote failed for {option_symbol}")


class MissingPriceOrderManager(StubOrderManager):
    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=None,
            ask=None,
            last=None,
            open_interest=550,
            outcome="SUCCESS",
        )


class ExpensiveQuoteOrderManager(StubOrderManager):
    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=8.90,
            ask=9.10,
            last=9.00,
            open_interest=550,
            outcome="SUCCESS",
        )


class LowCashOrderManager(StubOrderManager):
    async def get_portfolio(self):
        return {
            "buyingPower": {
                "cashOnlyBuyingPower": "150.00",
            }
        }


class MarginOrderManager(LowCashOrderManager):
    async def get_account_info(self):
        return {"brokerageAccountType": "MARGIN"}


def _cash_guard(order_manager, tmp_path) -> CashGuard:
    backend = SQLiteBackend(str(tmp_path / "bhiksha.db"))
    return CashGuard(
        order_manager=order_manager,
        repository=SQLiteCashBudgetRepository(str(tmp_path / "bhiksha.db"), backend=backend),
    )


def test_execution_planner_creates_dry_run_trade_plan():
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True))

    assert plan is not None
    assert plan.option_symbol == "QQQ260330P00558000"
    assert plan.order_id == "DRY_RUN"
    assert plan.estimated_entry_price == 2.85
    assert plan.risk_details["entry_pricing"]["bid"] == 2.70
    assert plan.risk_details["entry_pricing"]["ask"] == 2.90
    assert plan.risk_details["entry_pricing"]["mid"] == 2.80
    assert chain_service.calls == 1


def test_execution_planner_blocks_trade_outside_execution_window() -> None:
    deployment = historical_deployment("jerk_pivot_momentum_tsla_short_v1")
    chain_service = StubChainService()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="TSLA",
        timestamp=datetime(2026, 3, 30, 19, 45),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.option_symbol == ""
    assert plan.risk_reasons == ["execution_window_blocked"]
    assert chain_service.calls == 0


def test_execution_planner_can_simulate_without_tracking_position() -> None:
    deployment = historical_deployment("jerk_pivot_momentum_tsla_short_v1")
    chain_service = StubChainService(symbol="TSLA", option_symbol="TSLA260417P00250000", dte=17, delta=-0.45)
    tracker = PositionTracker()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="TSLA",
        timestamp=datetime(2026, 3, 30, 18, 0),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 250.25},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    assert plan.order_id is None
    assert plan.quantity > 0
    assert plan.underlying_entry_price == 250.25
    assert plan.entry_timestamp == decision.timestamp
    assert tracker.active_positions() == []


def test_execution_planner_shadow_ignores_book_position_caps() -> None:
    deployment = historical_deployment("jerk_pivot_momentum_tsla_short_v1")
    chain_service = StubChainService(symbol="TSLA", option_symbol="TSLA260417P00250000", dte=17, delta=-0.45)
    tracker = PositionTracker()
    tracker.open_position("IWM", "market_impulse_iwm_long_v1", trade_id="SHADOW-IWM", option_symbol="IWM260417C00210000")
    tracker.open_position("AVGO", "opening_drive_avgo_long_v1", trade_id="SHADOW-AVGO", option_symbol="AVGO260417C02000000")
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="TSLA",
        timestamp=datetime(2026, 3, 30, 18, 0),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 250.25},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    assert plan.quantity > 0
    assert plan.risk_reasons == ["approved"]
    assert plan.order_id is None
    assert len(tracker.active_positions()) == 2


def test_execution_planner_dry_run_still_honors_book_position_caps() -> None:
    deployment = historical_deployment("jerk_pivot_momentum_tsla_short_v1")
    chain_service = StubChainService(symbol="TSLA", option_symbol="TSLA260417P00250000", dte=17, delta=-0.45)
    tracker = PositionTracker()
    tracker.open_position("IWM", "market_impulse_iwm_long_v1", trade_id="DRY-IWM", option_symbol="IWM260417C00210000")
    tracker.open_position("AVGO", "opening_drive_avgo_long_v1", trade_id="DRY-AVGO", option_symbol="AVGO260417C02000000")
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="TSLA",
        timestamp=datetime(2026, 3, 30, 18, 0),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 250.25},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True))

    assert plan is not None
    assert plan.quantity > 0
    assert plan.risk_reasons == ["max_open_positions_total_reached"]
    assert plan.order_id is None
    assert len(tracker.active_positions()) == 2


def test_execution_planner_blocks_trade_when_quote_lookup_fails() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=QuoteErrorOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.option_symbol == "QQQ260330P00558000"
    assert plan.risk_reasons == ["public_quote_unavailable"]


def test_execution_planner_blocks_contract_already_owned_by_other_deployment() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    tracker = PositionTracker()
    tracker.open_position(
        "QQQ",
        "market_impulse_qqq_short_v2",
        trade_id="TRADE-OTHER",
        option_symbol="QQQ260330P00558000",
        quantity=1,
    )
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=StubOrderManager(),
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.option_symbol == "QQQ260330P00558000"
    assert plan.risk_reasons == ["option_contract_already_owned_by_other_deployment"]
    assert tracker.deployment_open_positions(deployment.deployment_id) == 0


def test_execution_planner_blocks_intrinsic_mismatch_from_bad_underlying_bar() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1").model_copy(update={"symbol": "MU"})
    deployment = deployment.model_copy(
        update={
            "strategy": deployment.strategy.model_copy(
                update={"params": {**deployment.strategy.params, "direction": "long"}}
            )
        }
    )
    planner = ExecutionPlanner(
        chain_service=StubChainService(
            symbol="MU",
            option_symbol="MU260605C00097000",
            contract_type="CALL",
            strike=97.0,
            dte=7,
            delta=0.25,
            bid=19.50,
            ask=19.85,
        ),
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="MU",
        timestamp=datetime(2026, 5, 26, 14, 6),
        signal=True,
        direction=SignalDirection.LONG,
        reason=["time_window_ok"],
        features={"close": 849.885},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.risk_reasons == ["underlying_option_price_inconsistent"]
    assert plan.risk_details["intrinsic_value"] > 700.0


def test_execution_planner_blocks_trade_when_quote_has_no_usable_price() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={"max_bid_ask_spread_pct": None}
            )
        }
    )
    planner = ExecutionPlanner(
        chain_service=StubChainService(
            symbol="QQQ",
            option_symbol="QQQ260330P00558000",
            dte=0,
            delta=-0.31,
            bid=None,
            ask=None,
        ),
        order_manager=MissingPriceOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.option_symbol == "QQQ260330P00558000"
    assert plan.risk_reasons == ["public_quote_missing_bid_ask"]


def test_execution_planner_blocks_trade_when_one_contract_exceeds_budget() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=ExpensiveQuoteOrderManager(),
        position_tracker=PositionTracker(),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True))

    assert plan is not None
    assert plan.quantity == 0
    assert plan.risk_reasons == ["insufficient_budget_for_single_contract"]
    assert plan.risk_details == {
        "reason": "insufficient_budget",
        "max_premium": 300.0,
        "entry_price": 9.1,
        "min_contract_cost": 910.0,
        "entry_pricing": plan.risk_details["entry_pricing"],
    }
    assert plan.risk_details["entry_pricing"]["selected_limit_price"] == 9.1


def test_execution_planner_blocks_live_trade_when_internal_cash_budget_is_insufficient(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    order_manager = LowCashOrderManager()
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=order_manager,
        position_tracker=PositionTracker(),
        cash_guard=_cash_guard(order_manager, tmp_path),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.order_id is None
    assert plan.risk_reasons == ["insufficient_internal_settled_cash_budget"]
    assert plan.risk_details == {
        "required_cash": 290.04,
        "buying_power_requirement": 290.04,
        "estimated_cost": 289.98,
        "entry_pricing": plan.risk_details["entry_pricing"],
        "remaining_budget": 142.5,
        "usable_budget": 142.5,
        "broker_cash_only_buying_power": 150.0,
        "buffer_pct": 0.05,
        "account_type": "CASH",
        "cash_guard_mode": "on",
    }
    assert plan.risk_details["entry_pricing"]["selected_limit_price"] == 2.85
    assert order_manager.place_entry_order_calls == 0


def test_execution_planner_auto_guard_skips_margin_accounts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "auto")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    order_manager = MarginOrderManager()
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=order_manager,
        position_tracker=PositionTracker(),
        cash_guard=_cash_guard(order_manager, tmp_path),
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.order_id == "OID123"
    assert order_manager.place_entry_order_calls == 1
