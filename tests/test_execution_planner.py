import asyncio
from datetime import datetime

from bhiksha.config.loader import load_deployments
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import OptionContractSnapshot, SignalDecision
from bhiksha.execution.order_manager import PublicQuote, PreflightCheck
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.state.position_tracker import PositionTracker


class StubChainService:
    async def get_chain(self, symbol: str, **kwargs):
        return [
            OptionContractSnapshot(
                option_symbol="QQQ260330P00558000",
                underlying_symbol="QQQ",
                contract_type="PUT",
                expiration_date="2026-03-30",
                dte=0,
                strike=558.0,
                delta=-0.31,
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
    planner = ExecutionPlanner(
        chain_service=StubChainService(),
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
