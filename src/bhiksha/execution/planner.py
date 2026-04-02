"""Trade planning from signals to dry-run/live execution."""

from __future__ import annotations

from datetime import time, timedelta
import uuid

from bhiksha.config.models import ConservativeRiskProfile, DeploymentManifest
from bhiksha.domain.models import OptionSelectionRequest, SignalDecision, TradePlan
from bhiksha.execution.order_manager import OrderManager, OrderResult
from bhiksha.integrations.schwab.chain import SchwabOptionChainService
from bhiksha.market_data.session import as_et_time
from bhiksha.options.vehicle_resolver import VehicleResolver
from bhiksha.risk.governor import RiskGovernor
from bhiksha.state.position_tracker import PositionTracker


class ExecutionPlanner:
    """Plan or place a Day 1 single-leg trade from a signal."""

    def __init__(
        self,
        *,
        chain_service: SchwabOptionChainService | None = None,
        vehicle_resolver: VehicleResolver | None = None,
        order_manager: OrderManager | None = None,
        position_tracker: PositionTracker | None = None,
    ) -> None:
        self.chain_service = chain_service or SchwabOptionChainService()
        self.vehicle_resolver = vehicle_resolver or VehicleResolver()
        self.order_manager = order_manager or OrderManager()
        self.position_tracker = position_tracker or PositionTracker()

    async def close(self) -> None:
        await self.chain_service.close()
        await self.order_manager.close()

    async def plan_entry(
        self,
        deployment: DeploymentManifest,
        decision: SignalDecision,
        *,
        dry_run: bool,
        simulate_only: bool = False,
    ) -> TradePlan | None:
        if not decision.signal or decision.direction is None:
            return None
        underlying_entry_price = _underlying_entry_price(decision)
        if not _entry_window_allows(deployment, decision.timestamp):
            return TradePlan(
                trade_id=str(uuid.uuid4()),
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol="",
                quantity=0,
                estimated_entry_price=0.0,
                risk_reasons=["execution_window_blocked"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )

        selection_request = OptionSelectionRequest(
            deployment_id=deployment.deployment_id,
            symbol=deployment.symbol,
            direction=decision.direction,
            signal_timestamp=decision.timestamp,
            execution_profile=deployment.execution.profile,
            execution_params={
                **deployment.execution.model_dump(),
                "long_signal_contract_type": deployment.execution.option_mapping.get("long_signal", "CALL"),
                "short_signal_contract_type": deployment.execution.option_mapping.get("short_signal", "PUT"),
            },
        )

        contracts = await self.chain_service.get_chain(
            deployment.symbol,
            contract_type="ALL",
            from_date=decision.timestamp.date(),
            to_date=(decision.timestamp + timedelta(days=deployment.execution.dte_max + 1)).date(),
        )
        selection = self.vehicle_resolver.resolve(selection_request, contracts)
        conflicting_positions = [
            position
            for position in self.position_tracker.find_by_option_symbol(selection.option_symbol)
            if position.deployment_id != deployment.deployment_id
        ]
        trade_id = str(uuid.uuid4())
        if conflicting_positions:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=selection.estimated_entry_price or 0.0,
                risk_reasons=["option_contract_already_owned_by_other_deployment"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )
        quote = await self.order_manager.get_option_quote(selection.option_symbol)
        entry_price = quote.entry_reference_price or selection.estimated_entry_price
        if entry_price is None:
            raise ValueError(f"Selected contract {selection.option_symbol} has no usable price")
        if quote.open_interest is not None and quote.open_interest < deployment.execution.min_open_interest:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["public_open_interest_below_minimum"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )
        if (
            deployment.execution.max_bid_ask_spread_pct is not None
            and quote.spread_pct is not None
            and quote.spread_pct > deployment.execution.max_bid_ask_spread_pct
        ):
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["public_spread_above_maximum"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )

        max_trade_premium = deployment.risk.max_trade_premium_usd or 300.0
        quantity = int(max_trade_premium // (entry_price * 100))
        if quantity <= 0:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["insufficient_budget_for_single_contract"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )
        risk_profile = ConservativeRiskProfile(
            profile=deployment.risk.profile,
            max_trade_premium_usd=max_trade_premium,
            hard_flat_time_et=deployment.risk.hard_flat_time_et or "15:55",
        )
        risk = RiskGovernor(risk_profile).check_entry(
            total_open_positions=self.position_tracker.total_open_positions,
            symbol_open_positions=self.position_tracker.symbol_open_positions(deployment.symbol),
            deployment_open_positions=self.position_tracker.deployment_open_positions(deployment.deployment_id),
            proposed_trade_premium_usd=entry_price * quantity * 100,
        )

        if not risk.approved:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=risk.reasons,
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )

        if dry_run:
            if simulate_only:
                return TradePlan(
                    trade_id=trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol=selection.option_symbol,
                    quantity=quantity,
                    estimated_entry_price=entry_price,
                    risk_reasons=risk.reasons,
                    dry_run=True,
                    order_id=None,
                )
            self.position_tracker.open_position(
                deployment.symbol,
                deployment.deployment_id,
                trade_id=trade_id,
                option_symbol=selection.option_symbol,
                    quantity=quantity,
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp,
                    source="dry_run",
                    order_id="DRY_RUN",
                )
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=risk.reasons,
                dry_run=True,
                order_id="DRY_RUN",
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )

        try:
            preflight = await self.order_manager.preflight_entry(selection.option_symbol, entry_price, quantity)
        except Exception as exc:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=[f"public_preflight_failed:{exc}"],
                dry_run=False,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )

        final_limit_price = float(preflight.payload["limitPrice"])
        result: OrderResult = await self.order_manager.place_entry_order(
            selection.option_symbol,
            final_limit_price,
            quantity,
            order_id=trade_id,
        )
        if result.order_id:
            self.position_tracker.open_position(
                deployment.symbol,
                deployment.deployment_id,
                trade_id=trade_id,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
                source="live_pending",
                order_id=result.order_id,
            )
        return TradePlan(
            trade_id=trade_id,
            deployment_id=deployment.deployment_id,
            symbol=deployment.symbol,
            direction=decision.direction,
            option_symbol=selection.option_symbol,
            quantity=quantity,
            estimated_entry_price=final_limit_price,
            risk_reasons=risk.reasons if result.order_id else [*risk.reasons, result.error or "order_submit_failed"],
            dry_run=False,
            order_id=result.order_id,
            underlying_entry_price=underlying_entry_price,
            entry_timestamp=decision.timestamp,
        )


def _entry_window_allows(deployment: DeploymentManifest, timestamp) -> bool:
    start = _parse_optional_et_time(deployment.execution.entry_window_start_et)
    end = _parse_optional_et_time(deployment.execution.entry_window_end_et)
    if start is None and end is None:
        return True
    current = as_et_time(timestamp)
    if start is not None and current < start:
        return False
    if end is not None and current > end:
        return False
    return True


def _underlying_entry_price(decision: SignalDecision) -> float | None:
    value = decision.features.get("close")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_optional_et_time(value: str | None) -> time | None:
    if value is None or not value.strip():
        return None
    return time.fromisoformat(value)
