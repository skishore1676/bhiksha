"""Trade planning from signals to dry-run/live execution."""

from __future__ import annotations

from datetime import timedelta

from bhiksha.config.models import ConservativeRiskProfile, DeploymentManifest
from bhiksha.domain.models import OptionSelectionRequest, SignalDecision, TradePlan
from bhiksha.execution.order_manager import OrderManager, OrderResult
from bhiksha.integrations.schwab.chain import SchwabOptionChainService
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
    ) -> TradePlan | None:
        if not decision.signal or decision.direction is None:
            return None

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
        quote = await self.order_manager.get_option_quote(selection.option_symbol)
        entry_price = quote.entry_reference_price or selection.estimated_entry_price
        if entry_price is None:
            raise ValueError(f"Selected contract {selection.option_symbol} has no usable price")
        if quote.open_interest is not None and quote.open_interest < deployment.execution.min_open_interest:
            return TradePlan(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["public_open_interest_below_minimum"],
                dry_run=dry_run,
                order_id=None,
            )
        if (
            deployment.execution.max_bid_ask_spread_pct is not None
            and quote.spread_pct is not None
            and quote.spread_pct > deployment.execution.max_bid_ask_spread_pct
        ):
            return TradePlan(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["public_spread_above_maximum"],
                dry_run=dry_run,
                order_id=None,
            )

        max_trade_premium = deployment.risk.max_trade_premium_usd or 300.0
        quantity = int(max_trade_premium // (entry_price * 100))
        if quantity <= 0:
            return TradePlan(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["insufficient_budget_for_single_contract"],
                dry_run=dry_run,
                order_id=None,
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
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=risk.reasons,
                dry_run=dry_run,
                order_id=None,
            )

        if dry_run:
            self.position_tracker.open_position(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                source="dry_run",
                order_id="DRY_RUN",
            )
            return TradePlan(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=risk.reasons,
                dry_run=True,
                order_id="DRY_RUN",
            )

        try:
            preflight = await self.order_manager.preflight_entry(selection.option_symbol, entry_price, quantity)
        except Exception as exc:
            return TradePlan(
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=[f"public_preflight_failed:{exc}"],
                dry_run=False,
                order_id=None,
            )

        final_limit_price = float(preflight.payload["limitPrice"])
        result: OrderResult = await self.order_manager.place_entry_order(
            selection.option_symbol,
            final_limit_price,
            quantity,
        )
        if result.order_id:
            self.position_tracker.open_position(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                source="live_pending",
                order_id=result.order_id,
            )
        return TradePlan(
            deployment_id=deployment.deployment_id,
            symbol=deployment.symbol,
            direction=decision.direction,
            option_symbol=selection.option_symbol,
            quantity=quantity,
            estimated_entry_price=final_limit_price,
            risk_reasons=risk.reasons if result.order_id else [*risk.reasons, result.error or "order_submit_failed"],
            dry_run=False,
            order_id=result.order_id,
        )
