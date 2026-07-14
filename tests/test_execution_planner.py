import asyncio
from datetime import date, datetime

import pytest

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, SignalDecision
from bhiksha.execution.order_manager import PublicQuote, PreflightCheck
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.options.selectors import SelectorEmptyError
from bhiksha.persistence.repository import ChainSnapshotRepository
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
        self.last_kwargs = None

    async def get_chain(self, symbol: str, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
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


def test_execution_planner_scales_initial_spread_fraction_by_chain_oi_percentile():
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "entry_pricing_spread_fraction": 0.25,
                    "entry_pricing_oi_percentile_scale": True,
                }
            )
        }
    )
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
    )

    plan = asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))

    assert plan is not None
    assert plan.estimated_entry_price == 2.75
    assert plan.risk_details["open_interest_percentile"] == 1.0
    assert plan.risk_details["entry_pricing"]["policy"]["spread_fraction"] == 0.25


def test_execution_planner_named_patient_profile_sets_authoritative_price_and_comparisons():
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={"entry_execution_profile": "patient", "entry_pricing_spread_fraction": None}
            )
        }
    )
    planner = ExecutionPlanner(
        chain_service=StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31),
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
    )

    plan = asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))

    pricing = plan.risk_details["entry_pricing"]
    assert plan.estimated_entry_price == 2.75
    assert pricing["entry_execution_profile"] == "patient"
    assert pricing["initial_profile_comparison"]["balanced"]["quote_limit_price"] == 2.77
    assert pricing["initial_profile_comparison"]["urgent"]["quote_limit_price"] == 2.80


def test_execution_planner_allow_nearest_after_extends_chain_lookup_and_records_selection_details():
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    deployment = deployment.model_copy(
        update={
            "execution": deployment.execution.model_copy(
                update={
                    "dte_min": 3,
                    "dte_max": 7,
                    "dte_fallback_policy": "allow_nearest_after",
                    "max_bid_ask_spread_pct": 0.20,
                }
            )
        }
    )
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260408P00558000", dte=9, delta=-0.31)
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

    assert chain_service.last_kwargs is not None
    assert chain_service.last_kwargs["to_date"] == date(2026, 4, 14)
    assert plan is not None
    assert plan.option_symbol == "QQQ260408P00558000"
    assert plan.risk_details["dte_fallback_policy"] == "allow_nearest_after"
    assert plan.risk_details["requested_dte_min"] == 3
    assert plan.risk_details["requested_dte_max"] == 7
    assert plan.risk_details["selected_dte"] == 9
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


def test_execution_planner_live_ignores_shadow_positions_for_capacity() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    tracker = PositionTracker()
    tracker.open_position(
        "AMD",
        "amd_shadow",
        trade_id="SHADOW-AMD",
        option_symbol="AMD260417P00460000",
        source="shadow",
        order_id="SHADOW_ENTRY",
    )
    tracker.open_position(
        "SMH",
        "smh_shadow",
        trade_id="SHADOW-SMH",
        option_symbol="SMH260417P00580000",
        source="shadow",
        order_id="SHADOW_ENTRY",
    )
    order_manager = StubOrderManager()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=order_manager,
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 558.0},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.risk_reasons == ["approved"]
    assert plan.order_id == "OID123"
    assert order_manager.place_entry_order_calls == 1
    assert tracker.total_open_positions == 3
    assert tracker.total_live_open_positions == 1


def test_execution_planner_live_honors_live_position_capacity() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    tracker = PositionTracker()
    tracker.open_position(
        "IWM",
        "iwm_live",
        trade_id="LIVE-IWM",
        option_symbol="IWM260417C00210000",
        source="live_pending",
        order_id="LIVE-IWM",
    )
    tracker.open_position(
        "NVDA",
        "nvda_live",
        trade_id="LIVE-NVDA",
        option_symbol="NVDA260417P00200000",
        source="live_pending",
        order_id="LIVE-NVDA",
    )
    order_manager = StubOrderManager()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=order_manager,
        position_tracker=tracker,
    )
    decision = SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={"close": 558.0},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=False))

    assert plan is not None
    assert plan.risk_reasons == ["max_open_positions_total_reached"]
    assert plan.order_id is None
    assert order_manager.place_entry_order_calls == 0


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
        "selected_open_interest": 500,
        "open_interest_percentile": 1.0,
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
        "selected_open_interest": 500,
        "open_interest_percentile": 1.0,
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


class SpyChainSnapshotRepository(ChainSnapshotRepository):
    """Captures every recorded attempt in memory for assertions."""

    def __init__(self) -> None:
        self.attempts = []

    async def record_attempt(self, attempt) -> None:
        self.attempts.append(attempt)

    async def purge_older_than(self, cutoff) -> int:
        del cutoff
        return 0


class RaisingChainSnapshotRepository(ChainSnapshotRepository):
    """Simulates a broken snapshot write to prove entry survives it."""

    async def record_attempt(self, attempt) -> None:
        del attempt
        raise RuntimeError("disk full")

    async def purge_older_than(self, cutoff) -> int:
        del cutoff
        raise RuntimeError("disk full")


def _short_decision(deployment) -> SignalDecision:
    return SignalDecision(
        deployment_id=deployment.deployment_id,
        symbol="QQQ",
        timestamp=datetime(2026, 3, 30, 14, 30),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok"],
        features={},
    )


def test_execution_planner_records_chain_snapshot_on_successful_selection() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    snapshot_repository = SpyChainSnapshotRepository()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
        chain_snapshot_repository=snapshot_repository,
    )

    plan = asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))

    assert plan is not None
    assert len(snapshot_repository.attempts) == 1
    attempt = snapshot_repository.attempts[0]
    assert attempt.deployment_id == deployment.deployment_id
    assert attempt.symbol == "QQQ"
    assert attempt.lane == "dry_run"
    assert attempt.selector_empty is False
    assert attempt.selected_option_symbol == "QQQ260330P00558000"
    assert len(attempt.rows) == 1
    assert attempt.rows[0].is_selected is True
    assert attempt.rows[0].verdict == "accepted"


def test_execution_planner_records_chain_snapshot_on_selector_empty() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    # min_open_interest is 100 for this fixture deployment (see
    # tests/fixtures/config/deployments/market_impulse_qqq_short_v1.yaml) --
    # open_interest=5 forces every candidate to fail that filter.
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    chain_service.get_chain = _low_oi_get_chain(chain_service)
    snapshot_repository = SpyChainSnapshotRepository()
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
        chain_snapshot_repository=snapshot_repository,
    )

    with pytest.raises(SelectorEmptyError):
        asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))

    assert len(snapshot_repository.attempts) == 1
    attempt = snapshot_repository.attempts[0]
    assert attempt.selector_empty is True
    assert attempt.selected_option_symbol is None
    assert len(attempt.rows) == 1
    assert attempt.rows[0].verdict == "open_interest_below_min"
    assert attempt.rows[0].is_selected is False


def test_execution_planner_snapshot_repository_failure_does_not_break_entry() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
        chain_snapshot_repository=RaisingChainSnapshotRepository(),
    )

    plan = asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))

    assert plan is not None
    assert plan.option_symbol == "QQQ260330P00558000"
    assert plan.order_id == "DRY_RUN"


def test_execution_planner_snapshot_repository_failure_does_not_mask_selector_empty() -> None:
    deployment = _enabled_deployment("market_impulse_qqq_short_v1")
    chain_service = StubChainService(symbol="QQQ", option_symbol="QQQ260330P00558000", dte=0, delta=-0.31)
    chain_service.get_chain = _low_oi_get_chain(chain_service)
    planner = ExecutionPlanner(
        chain_service=chain_service,
        order_manager=StubOrderManager(),
        position_tracker=PositionTracker(),
        chain_snapshot_repository=RaisingChainSnapshotRepository(),
    )

    with pytest.raises(SelectorEmptyError):
        asyncio.run(planner.plan_entry(deployment, _short_decision(deployment), dry_run=True))


def _low_oi_get_chain(chain_service: StubChainService):
    original_get_chain = StubChainService.get_chain

    async def get_chain(symbol: str, **kwargs):
        contracts = await original_get_chain(chain_service, symbol, **kwargs)
        return [
            OptionContractSnapshot(
                option_symbol=contract.option_symbol,
                underlying_symbol=contract.underlying_symbol,
                contract_type=contract.contract_type,
                expiration_date=contract.expiration_date,
                dte=contract.dte,
                strike=contract.strike,
                delta=contract.delta,
                bid=contract.bid,
                ask=contract.ask,
                open_interest=5,
            )
            for contract in contracts
        ]

    return get_chain
