import asyncio
from datetime import datetime

from bhiksha.config.loader import load_deployments
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, SignalDecision
from bhiksha.execution.order_manager import PublicQuote, PreflightCheck
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository
from bhiksha.risk.cash_guard import CashGuard
from bhiksha.state.position_tracker import PositionTracker


class StubChainService:
    def __init__(
        self,
        *,
        symbol: str = "QQQ",
        option_symbol: str | None = None,
        dte: int = 0,
        delta: float = -0.31,
        bid: float | None = 3.00,
        ask: float | None = 2.90,
    ) -> None:
        self.calls = 0
        self.symbol = symbol
        self.option_symbol = option_symbol or f"{symbol}260330P00558000"
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
                contract_type="PUT",
                expiration_date="2026-03-30",
                dte=self.dte,
                strike=558.0,
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
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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
    assert plan.estimated_entry_price == 2.90
    assert chain_service.calls == 1


def test_execution_planner_blocks_trade_outside_execution_window() -> None:
    deployment = next(
        d for d in load_deployments("config/deployments") if d.deployment_id == "jerk_pivot_momentum_tsla_short_v1"
    )
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
    deployment = next(
        d for d in load_deployments("config/deployments") if d.deployment_id == "jerk_pivot_momentum_tsla_short_v1"
    )
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


def test_execution_planner_blocks_trade_when_quote_lookup_fails() -> None:
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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


def test_execution_planner_blocks_trade_when_quote_has_no_usable_price() -> None:
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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
    assert plan.risk_reasons == ["public_quote_missing_price"]


def test_execution_planner_blocks_trade_when_one_contract_exceeds_budget() -> None:
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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
    }


def test_execution_planner_blocks_live_trade_when_internal_cash_budget_is_insufficient(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "on")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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
        "remaining_budget": 142.5,
        "usable_budget": 142.5,
        "broker_cash_only_buying_power": 150.0,
        "buffer_pct": 0.05,
        "account_type": "CASH",
        "cash_guard_mode": "on",
    }
    assert order_manager.place_entry_order_calls == 0


def test_execution_planner_auto_guard_skips_margin_accounts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_MODE", "auto")
    monkeypatch.setenv("BHIKSHA_CASH_GUARD_BUFFER_PCT", "0.05")
    deployment = next(d for d in load_deployments("config/deployments") if d.deployment_id == "market_impulse_qqq_short_v1")
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
