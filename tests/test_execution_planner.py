import asyncio
from datetime import datetime

from bhiksha.config.loader import load_deployments
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, SignalDecision
from bhiksha.execution.order_manager import PublicQuote, PreflightCheck
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.state.position_tracker import PositionTracker


class StubChainService:
    def __init__(self, *, symbol: str = "QQQ", option_symbol: str | None = None, dte: int = 0, delta: float = -0.31) -> None:
        self.calls = 0
        self.symbol = symbol
        self.option_symbol = option_symbol or f"{symbol}260330P00558000"
        self.dte = dte
        self.delta = delta

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
                bid=3.00,
                ask=2.90,
                open_interest=500,
            )
        ]

    async def close(self):
        return None


class StubOrderManager:
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

    async def place_entry_order(self, option_symbol: str, limit_price: float, quantity: int):
        class Result:
            order_id = "OID123"
            error = None
        return Result()

    async def close(self):
        return None


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
        features={},
    )

    plan = asyncio.run(planner.plan_entry(deployment, decision, dry_run=True, simulate_only=True))

    assert plan is not None
    assert plan.order_id is None
    assert plan.quantity > 0
    assert tracker.active_positions() == []
