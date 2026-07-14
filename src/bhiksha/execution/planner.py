"""Trade planning from signals to dry-run/live execution."""

from __future__ import annotations

from datetime import time, timedelta
import uuid

from bhiksha.config.models import ConservativeRiskProfile, DeploymentManifest
from bhiksha.domain.models import OptionSelectionRequest, SignalDecision, TradePlan
from bhiksha.execution.order_manager import OrderManager, OrderResult
from bhiksha.execution.pricing import (
    build_entry_profile_comparison,
    resolve_initial_spread_fraction,
    scale_spread_fraction,
    select_entry_limit,
)
from bhiksha.market_data.session import as_et_time
from bhiksha.options.chain_service import OptionChainService
from bhiksha.options.chain_snapshot import build_chain_snapshot
from bhiksha.options.public_chain import PublicOptionChainService
from bhiksha.options.selectors import SelectorEmptyError
from bhiksha.options.vehicle_resolver import VehicleResolver
from bhiksha.persistence.repository import ChainSnapshotRepository, NullChainSnapshotRepository
from bhiksha.risk.cash_guard import CashGuard
from bhiksha.risk.governor import RiskGovernor
from bhiksha.state.position_tracker import PositionTracker
from bhiksha.time_utils import parse_time_text


DTE_FALLBACK_LOOKAHEAD_DAYS = 7


class ExecutionPlanner:
    """Plan or place a Day 1 single-leg trade from a signal."""

    def __init__(
        self,
        *,
        chain_service: OptionChainService | None = None,
        vehicle_resolver: VehicleResolver | None = None,
        order_manager: OrderManager | None = None,
        position_tracker: PositionTracker | None = None,
        cash_guard: CashGuard | None = None,
        chain_snapshot_repository: ChainSnapshotRepository | None = None,
    ) -> None:
        self.chain_service = chain_service or PublicOptionChainService()
        self.vehicle_resolver = vehicle_resolver or VehicleResolver()
        self.order_manager = order_manager or OrderManager()
        self.position_tracker = position_tracker or PositionTracker()
        self.cash_guard = cash_guard
        self.chain_snapshot_repository = chain_snapshot_repository or NullChainSnapshotRepository()

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

        dte_fallback_policy = str(deployment.execution.dte_fallback_policy or "strict").strip().lower()
        dte_lookup_padding_days = 1
        if dte_fallback_policy == "allow_nearest_after":
            dte_lookup_padding_days += DTE_FALLBACK_LOOKAHEAD_DAYS

        contracts = await self.chain_service.get_chain(
            deployment.symbol,
            contract_type="ALL",
            from_date=decision.timestamp.date(),
            to_date=(decision.timestamp + timedelta(days=deployment.execution.dte_max + dte_lookup_padding_days)).date(),
        )
        lane = "shadow" if simulate_only else ("dry_run" if dry_run else "live")
        snapshot_id = str(uuid.uuid4())
        try:
            selection = self.vehicle_resolver.resolve(selection_request, contracts)
        except SelectorEmptyError as exc:
            await self._capture_chain_snapshot(
                snapshot_id,
                selection_request,
                contracts,
                lane=lane,
                selection=None,
                selector_error=exc,
            )
            raise
        await self._capture_chain_snapshot(
            snapshot_id,
            selection_request,
            contracts,
            lane=lane,
            selection=selection,
            selector_error=None,
        )
        selection_details = _selection_details(selection)
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
        try:
            quote = await self.order_manager.get_option_quote(selection.option_symbol)
        except Exception:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=selection.estimated_entry_price or 0.0,
                risk_reasons=["public_quote_unavailable"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
            )
        execution_params = deployment.execution.model_dump()
        base_spread_fraction, active_entry_profile = resolve_initial_spread_fraction(execution_params)
        if base_spread_fraction is not None:
            execution_params["entry_pricing_spread_fraction"] = scale_spread_fraction(
                base_spread_fraction,
                enabled=deployment.execution.entry_pricing_oi_percentile_scale or active_entry_profile is not None,
                open_interest_percentile=selection.open_interest_percentile,
            )
        pricing = select_entry_limit(quote, execution_params)
        pricing_evidence = {
            **pricing.evidence(),
            "entry_execution_profile": active_entry_profile.name if active_entry_profile is not None else "legacy",
            "initial_profile_comparison": build_entry_profile_comparison(
                quote,
                deployment.execution.model_dump(),
                open_interest_percentile=selection.open_interest_percentile,
            ),
        }
        entry_price = pricing.limit_price
        if pricing.block_reasons:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=selection.estimated_entry_price or 0.0,
                risk_reasons=pricing.block_reasons,
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
                risk_details={"entry_pricing": pricing_evidence, **selection_details},
            )
        if entry_price is None:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=selection.estimated_entry_price or 0.0,
                risk_reasons=["public_quote_missing_price"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
                risk_details={"entry_pricing": pricing_evidence, **selection_details},
            )
        intrinsic_value = _intrinsic_value(
            contract_type=selection.contract_type,
            strike=selection.strike,
            underlying_price=underlying_entry_price,
        )
        if intrinsic_value is not None and entry_price + 0.05 < intrinsic_value:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=0,
                estimated_entry_price=entry_price,
                risk_reasons=["underlying_option_price_inconsistent"],
                dry_run=dry_run,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
                risk_details={
                    "underlying_entry_price": underlying_entry_price,
                    "contract_type": selection.contract_type,
                    "strike": selection.strike,
                    "entry_price": entry_price,
                    "intrinsic_value": intrinsic_value,
                    "entry_pricing": pricing_evidence,
                    **selection_details,
                },
            )

        max_trade_premium = deployment.risk.max_trade_premium_usd or 300.0
        min_contract_cost = entry_price * 100
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
                risk_details={
                    "reason": "insufficient_budget",
                    "max_premium": max_trade_premium,
                    "entry_price": entry_price,
                    "min_contract_cost": min_contract_cost,
                    "entry_pricing": pricing_evidence,
                    **selection_details,
                },
            )
        risk_profile = _risk_profile_for_deployment(deployment, max_trade_premium=max_trade_premium)
        if dry_run:
            total_open_positions = self.position_tracker.total_open_positions
            symbol_open_positions = self.position_tracker.symbol_open_positions(deployment.symbol)
            deployment_open_positions = self.position_tracker.deployment_open_positions(deployment.deployment_id)
        else:
            total_open_positions = self.position_tracker.total_live_open_positions
            symbol_open_positions = self.position_tracker.live_symbol_open_positions(deployment.symbol)
            deployment_open_positions = self.position_tracker.live_deployment_open_positions(deployment.deployment_id)
        risk = RiskGovernor(risk_profile).check_entry(
            total_open_positions=total_open_positions,
            symbol_open_positions=symbol_open_positions,
            deployment_open_positions=deployment_open_positions,
            proposed_trade_premium_usd=entry_price * quantity * 100,
            enforce_total_position_limit=not simulate_only,
            enforce_symbol_position_limit=not simulate_only,
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
                risk_details={"entry_pricing": pricing_evidence, **selection_details},
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
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp,
                    risk_details={"entry_pricing": pricing_evidence, **selection_details},
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
                risk_details={"entry_pricing": pricing_evidence, **selection_details},
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
                risk_details={"entry_pricing": pricing_evidence, **selection_details},
            )

        final_limit_price = float(preflight.payload["limitPrice"])
        pricing_evidence = {
            **pricing_evidence,
            "preflight_limit_price": final_limit_price,
            "preflight_increment": preflight.current_increment,
            "preflight_buying_power_requirement": preflight.buying_power_requirement,
            "preflight_estimated_cost": preflight.estimated_cost,
        }
        required_cash = max(
            preflight.buying_power_requirement or 0.0,
            preflight.estimated_cost or 0.0,
            final_limit_price * quantity * 100,
        )
        cash_guard_details: dict[str, object] = {}
        if self.cash_guard is not None:
            cash_guard_result = await self.cash_guard.reserve_entry(
                trade_id=trade_id,
                required_cash=required_cash,
                timestamp=decision.timestamp,
            )
            cash_guard_details = dict(cash_guard_result.details)
            if cash_guard_result.blocked:
                return TradePlan(
                    trade_id=trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol=selection.option_symbol,
                    quantity=quantity,
                    estimated_entry_price=final_limit_price,
                    risk_reasons=[cash_guard_result.reason or "cash_guard_blocked"],
                    dry_run=False,
                    order_id=None,
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp,
                    risk_details={
                        "required_cash": required_cash,
                        "buying_power_requirement": preflight.buying_power_requirement,
                        "estimated_cost": preflight.estimated_cost,
                        "entry_pricing": pricing_evidence,
                        **selection_details,
                        **cash_guard_details,
                    },
                )
        result: OrderResult = await self.order_manager.place_entry_order(
            selection.option_symbol,
            final_limit_price,
            quantity,
            order_id=trade_id,
        )
        if self.cash_guard is not None and (result.order_id is None or result.error):
            await self.cash_guard.release_entry(trade_id)
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
            risk_details={
                "required_cash": required_cash,
                "buying_power_requirement": preflight.buying_power_requirement,
                "estimated_cost": preflight.estimated_cost,
                "entry_pricing": pricing_evidence,
                **selection_details,
                **cash_guard_details,
            },
        )

    async def _capture_chain_snapshot(
        self,
        snapshot_id: str,
        selection_request: OptionSelectionRequest,
        contracts: list,
        *,
        lane: str,
        selection,
        selector_error,
    ) -> None:
        """Persist the candidate chain for one selection attempt.

        Telemetry only -- must never raise into ``plan_entry``. The candidate
        chain with its computed per-contract attributes (delta, spread, OI)
        already exists in memory at this point (it is the SAME ``contracts``
        list ``vehicle_resolver.resolve`` was just given); this makes no new
        network calls, and building + writing the bounded snapshot is a
        sub-millisecond, in-process SQLite transaction (~0.45ms measured for
        a 300-row attempt), so it is awaited inline rather than detached --
        see options/chain_snapshot.py for the capture/verdict logic and
        persistence/sqlite.py for the write path.
        """
        try:
            attempt = build_chain_snapshot(
                selection_request,
                contracts,
                lane=lane,
                snapshot_id=snapshot_id,
                selection=selection,
                selector_error=selector_error,
            )
            await self.chain_snapshot_repository.record_attempt(attempt)
        except Exception:
            # A snapshot failure must never break (or even flag) the entry
            # path -- the trade matters more than the telemetry.
            return


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


def _risk_profile_for_deployment(
    deployment: DeploymentManifest,
    *,
    max_trade_premium: float,
) -> ConservativeRiskProfile:
    payload = {
        "profile": deployment.risk.profile,
        "max_trade_premium_usd": max_trade_premium,
        "hard_flat_time_et": deployment.risk.hard_flat_time_et or "15:55",
    }
    for field_name in (
        "max_open_positions_total",
        "max_open_positions_per_symbol",
        "max_open_positions_per_deployment",
    ):
        value = getattr(deployment.risk, field_name)
        if value is not None:
            payload[field_name] = value
    return ConservativeRiskProfile(**payload)


def _selection_details(selection) -> dict:
    details = {
        "selected_open_interest": selection.open_interest,
        "open_interest_percentile": selection.open_interest_percentile,
    }
    if selection.dte_fallback_policy is not None:
        details.update(
            {
                "dte_fallback_policy": selection.dte_fallback_policy,
                "requested_dte_min": selection.requested_dte_min,
                "requested_dte_max": selection.requested_dte_max,
                "selected_dte": selection.dte,
            }
        )
    return details


def _underlying_entry_price(decision: SignalDecision) -> float | None:
    value = decision.features.get("close")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _intrinsic_value(
    *,
    contract_type: str | None,
    strike: float | None,
    underlying_price: float | None,
) -> float | None:
    if strike is None or underlying_price is None:
        return None
    normalized = str(contract_type or "").upper()
    if normalized == "CALL":
        return max(float(underlying_price) - float(strike), 0.0)
    if normalized == "PUT":
        return max(float(strike) - float(underlying_price), 0.0)
    return None


def _parse_optional_et_time(value: str | None) -> time | None:
    if value is None or not value.strip():
        return None
    return parse_time_text(value)
