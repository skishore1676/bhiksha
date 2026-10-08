"""Trade planning from signals to dry-run/live execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
import uuid
import time as clock

from bhiksha.config.models import ConservativeRiskProfile, DeploymentManifest
from bhiksha.domain.models import OptionSelectionRequest, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.native_orders import (
    decide_execution_route,
)
from bhiksha.execution.order_manager import OrderManager, OrderResult, preflight_failure_is_transient
from bhiksha.execution.quote_lineage import parse_provider_timestamp
from bhiksha.execution.pricing import (
    build_entry_profile_comparison,
    resolve_entry_reprice_max_chase_pct,
    resolve_initial_spread_fraction,
    select_entry_limit,
    ENTRY_POLICY_VERSION,
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
from bhiksha.risk.planned_loss import planned_stop_loss_usd, resolve_planned_stop_loss_pct
from bhiksha.risk.risk_manager import RiskManager
from bhiksha.state.position_tracker import PositionTracker
from bhiksha.time_utils import parse_time_text


DTE_FALLBACK_LOOKAHEAD_DAYS = 7


class _SizingBlocked(Exception):
    pass


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
        risk_manager: RiskManager | None = None,
        chain_snapshot_repository: ChainSnapshotRepository | None = None,
        entry_intent_repository=None,
    ) -> None:
        self.chain_service = chain_service or PublicOptionChainService()
        self.vehicle_resolver = vehicle_resolver or VehicleResolver()
        self.order_manager = order_manager or OrderManager()
        self.position_tracker = position_tracker or PositionTracker()
        self.cash_guard = cash_guard
        self.risk_manager = risk_manager
        self.chain_snapshot_repository = chain_snapshot_repository or NullChainSnapshotRepository()
        self.entry_intent_repository = entry_intent_repository

    async def _fit_live_quantity(self, *, trade_id, deployment, timestamp, price,
                                 upper_quantity, premium_cap, extra_cash=0.0):
        """Advisory sizing from existing owners; no relaxation of final reservations."""
        details = {"requested_quantity": upper_quantity}
        quantity = min(upper_quantity, int(premium_cap // (price * 100)))
        if self.cash_guard is not None:
            cash = await self.cash_guard.preview_entry(trade_id=trade_id, timestamp=timestamp)
            details["cash_capacity"] = dict(cash.details)
            if cash.blocked:
                return 0, cash.reason, details
            if cash.enforced:
                capacity = cash.details.get("remaining_budget")
                if capacity is None:
                    return 0, "cash_guard_cash_unavailable", details
                quantity = min(quantity, int(max(0, float(capacity) - extra_cash) // (price * 100)))
                if quantity < 1:
                    return 0, "insufficient_internal_settled_cash_budget", details
        if self.risk_manager is not None:
            stop, _ = resolve_planned_stop_loss_pct(deployment)
            risk = await self.risk_manager.preview_sized_entry(
                trade_id=trade_id, deployment_id=deployment.deployment_id,
                symbol=deployment.symbol, entry_price=price, quantity=max(1, quantity), stop_loss_pct=stop)
            details["risk_capacity"] = dict(risk.details)
            if not risk.allowed and risk.reason != "risk_prospective_loss_headroom_exceeded":
                return 0, risk.reason, details
            headroom = risk.details.get("remaining_loss_headroom_usd")
            if headroom is not None:
                # Compare rounded loss for the whole quantity, including exact boundaries.
                low, high = 0, quantity
                while low < high:
                    candidate = (low + high + 1) // 2
                    loss = planned_stop_loss_usd(entry_price=price, quantity=candidate, stop_loss_pct=stop)
                    if loss is not None and loss <= float(headroom):
                        low = candidate
                    else:
                        high = candidate - 1
                quantity = low
                if quantity < 1:
                    return 0, "risk_prospective_loss_headroom_exceeded", details
            elif not risk.allowed:
                return 0, risk.reason, details
        details["selected_quantity"] = quantity
        return quantity, None if quantity > 0 else "insufficient_budget_for_single_contract", details

    async def close(self) -> None:
        await self.chain_service.close()
        await self.order_manager.close()

    async def plan_entry(self, deployment, decision, *, dry_run, simulate_only=False, entry_guard=None, pricing_references=None):
        """Bounded recovery before submission, within one frozen chain cohort."""
        context = {"candidate_rank": 0, "pricing_references": dict(pricing_references or {})}
        attempts = []
        fresh_rebuild_used = False
        previous = None
        for _ in range(6):  # Five candidates plus at most one final-freshness rebuild.
            try:
                plan = await self._plan_entry_once(deployment, decision, dry_run=dry_run,
                    simulate_only=simulate_only, entry_guard=entry_guard, _context=context)
            except SelectorEmptyError:
                if previous is None:
                    raise
                previous.risk_details["candidate_search_exhausted"] = True
                plan = previous
                break
            if plan is None:
                return None
            attempts.append({"trade_id": plan.trade_id, "option_symbol": plan.option_symbol,
                "candidate_rank": context["candidate_rank"], "quantity": plan.quantity,
                "price": plan.estimated_entry_price, "reasons": plan.risk_reasons})
            previous = plan
            reasons = set(plan.risk_reasons) - {"approved"}
            pr = plan.risk_details.get("entry_pricing") or {}
            final = pr.get("final_quote_validation") or {}
            if plan.order_id or not reasons:
                break
            if (not fresh_rebuild_used and reasons <= {"public_quote_stale_or_unproven", "public_quote_timestamp_missing"}
                and final.get("quote_timestamp_status") != "current" and final
                and pr.get("final_quote_validation_stage") == "before_submission"):
                fresh_rebuild_used = True
                context["rebuilding_final_quote"] = True
                continue
            contract_blocks = {"insufficient_budget_for_single_contract", "option_contract_already_owned_by_other_deployment",
                "public_quote_stale_or_unproven", "public_quote_timestamp_missing", "public_quote_unavailable",
                "public_quote_crossed_bid_ask", "public_quote_nonfinite", "public_open_interest_missing",
                "public_quote_missing_bid_ask", "public_spread_absurd", "public_spread_unavailable",
                "risk_prospective_loss_headroom_exceeded", "insufficient_internal_settled_cash_budget"}
            if not reasons or not reasons <= contract_blocks or context["candidate_rank"] >= 4:
                break
            context["candidate_rank"] += 1
        plan.risk_details["entry_pricing_references"] = context["pricing_references"]
        plan.risk_details["entry_recovery_attempts"] = attempts
        plan.risk_details["final_quote_rebuild_used"] = fresh_rebuild_used
        return plan

    async def _plan_entry_once(
        self,
        deployment: DeploymentManifest,
        decision: SignalDecision,
        *,
        dry_run: bool,
        simulate_only: bool = False,
        entry_guard: Callable[[], str | None] | None = None,
        _context: dict | None = None,
    ) -> TradePlan | None:
        if not decision.signal or decision.direction is None:
            return None
        _context = _context or {}
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
                "_entry_candidate_rank": _context.get("candidate_rank", 0),
                "long_signal_contract_type": deployment.execution.option_mapping.get("long_signal", "CALL"),
                "short_signal_contract_type": deployment.execution.option_mapping.get("short_signal", "PUT"),
            },
        )

        dte_fallback_policy = str(deployment.execution.dte_fallback_policy or "strict").strip().lower()
        dte_lookup_padding_days = 1
        if dte_fallback_policy == "allow_nearest_after":
            dte_lookup_padding_days += DTE_FALLBACK_LOOKAHEAD_DAYS

        lookup_dte_max = deployment.execution.dte_max + dte_lookup_padding_days
        if dte_fallback_policy == "allow_nearest_after" and deployment.execution.dte_fallback_max is not None:
            lookup_dte_max = deployment.execution.dte_fallback_max + 1
        contracts = _context.get("contracts")
        if contracts is None:
            contracts = await self.chain_service.get_chain(
                deployment.symbol,
                contract_type="ALL",
                from_date=decision.timestamp.date(),
                to_date=(decision.timestamp + timedelta(days=lookup_dte_max)).date(),
            )
            _context["contracts"] = contracts
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
        snapshot_attempt, snapshot_persisted = await self._capture_chain_snapshot(
            snapshot_id,
            selection_request,
            contracts,
            lane=lane,
            selection=selection,
            selector_error=None,
        )
        route = decide_execution_route(deployment)
        selection_details = {
            **_selection_details(selection),
            **_selection_snapshot_details(snapshot_attempt, persisted=snapshot_persisted),
        }
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
            quote_fetch_started_at = datetime.now(UTC)
            quote_fetch_started = clock.monotonic()
            quote = await self.order_manager.get_option_quote(selection.option_symbol)
            quote_received_at = datetime.now(UTC)
            quote_fetch_seconds = clock.monotonic() - quote_fetch_started
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
        _, active_entry_profile = resolve_initial_spread_fraction(execution_params)
        # Seek a normal fresh market only within the original top-five cohort.
        # Quote requests are cached for this attempt; never switch after submission.
        if select_entry_limit(quote, execution_params).wide_market:
            alternatives = []
            for rank in range(_context.get("candidate_rank", 0) + 1, 5):
                try:
                    request = replace(selection_request, execution_params={
                        **selection_request.execution_params, "_entry_candidate_rank": rank})
                    candidate = self.vehicle_resolver.resolve(request, contracts)
                except SelectorEmptyError:
                    break
                if self.position_tracker.find_by_option_symbol(candidate.option_symbol):
                    continue
                try:
                    started_at = datetime.now(UTC)
                    started_clock = clock.monotonic()
                    candidate_quote = await self.order_manager.get_option_quote(candidate.option_symbol)
                    received_at = datetime.now(UTC)
                    result = select_entry_limit(candidate_quote, execution_params, observed_at=received_at)
                    alternatives.append({"option_symbol": candidate.option_symbol, "rank": rank,
                        "wide_market": result.wide_market, "block_reasons": result.block_reasons})
                    if result.approved and not result.wide_market:
                        selection, quote = candidate, candidate_quote
                        quote_fetch_started_at, quote_received_at = started_at, received_at
                        quote_fetch_seconds = clock.monotonic() - started_clock
                        _context["candidate_rank"] = rank
                        snapshot_attempt, snapshot_persisted = await self._capture_chain_snapshot(
                            snapshot_id, request, contracts, lane=lane, selection=selection, selector_error=None)
                        selection_details = {**_selection_details(selection),
                            **_selection_snapshot_details(snapshot_attempt, persisted=snapshot_persisted)}
                        break
                except Exception as exc:
                    alternatives.append({"option_symbol": candidate.option_symbol, "rank": rank,
                        "error_type": type(exc).__name__})
            selection_details["narrower_market_search"] = alternatives
        # Freeze each contract's first usable midpoint/ceiling for the entire attempt,
        # including a final-freshness rebuild. A rejected quote never seeds a ceiling.
        frozen = _context.setdefault("pricing_references", {}).get(selection.option_symbol)
        if frozen:
            execution_params.update(frozen)
        pricing = select_entry_limit(quote, execution_params)
        def attempt_evidence(result, started_at, received_at, fetch_seconds):
            return {**result.evidence(), "quote_fetch_started_at": started_at.isoformat(),
                "quote_received_at": received_at.isoformat(), "quote_fetch_seconds": fetch_seconds,
                "fetch_to_pricing_seconds": (result.observed_at - received_at).total_seconds()}

        quote_attempts = [attempt_evidence(pricing, quote_fetch_started_at, quote_received_at, quote_fetch_seconds)]
        refreshed_quote = False

        def guarded_plan(*, check_freshness: bool = False, before_submission: bool = False) -> TradePlan | None:
            reason = entry_guard() if entry_guard is not None else None
            current = datetime.now(UTC)
            final_quote_validation = None
            if reason is None and (refreshed_quote or deployment.strategy.key == "weekly_chart" or _context.get("candidate_rank") or _context.get("rebuilding_final_quote")) and not _entry_window_allows(deployment, current):
                reason = "execution_window_blocked"
            if reason is None and deployment.strategy.key == "weekly_chart":
                from bhiksha.strategy.weekly_chart import pending_block
                reason = pending_block(deployment, current)
            if reason is None and check_freshness and (refreshed_quote or not dry_run or simulate_only):
                fresh_pricing = select_entry_limit(quote, execution_params, observed_at=current)
                final_quote_validation = fresh_pricing.evidence()
                pricing_evidence["final_quote_validation"] = final_quote_validation
                pricing_evidence["final_quote_validation_stage"] = "before_submission" if before_submission else "before_preflight"
                reason = next(iter(fresh_pricing.block_reasons), None)
                if reason is None and not dry_run:
                    status = final_quote_validation['quote_timestamp_status']
                    if status != 'current':
                        reason = ('public_quote_timestamp_missing' if status == 'missing'
                                  else 'public_quote_stale_or_unproven')
            if reason is None:
                return None
            return TradePlan(trade_id=trade_id, deployment_id=deployment.deployment_id,
                symbol=deployment.symbol, direction=decision.direction,
                option_symbol=selection.option_symbol, quantity=0,
                estimated_entry_price=pricing.limit_price or selection.estimated_entry_price or 0.0,
                risk_reasons=[reason], dry_run=dry_run,
                underlying_entry_price=underlying_entry_price, entry_timestamp=decision.timestamp,
                risk_details={"entry_pricing": {**pricing.evidence(), "quote_attempts": quote_attempts,
                    "final_quote_validation": final_quote_validation,
                    "final_quote_validation_stage": "before_submission" if before_submission else "before_preflight"},
                    "entry_permission_checked_at": current.isoformat(), **selection_details})

        timestamp_blocks = {"public_quote_stale_or_unproven", "public_quote_timestamp_missing"}
        if pricing.block_reasons and set(pricing.block_reasons) <= timestamp_blocks:
            refreshed_quote = True
            guarded = guarded_plan()
            if guarded is not None:
                return guarded
            try:
                quote_fetch_started_at = datetime.now(UTC)
                quote_fetch_started = clock.monotonic()
                quote = await self.order_manager.get_option_quote(selection.option_symbol)
                quote_received_at = datetime.now(UTC)
                quote_fetch_seconds = clock.monotonic() - quote_fetch_started
                pricing = select_entry_limit(quote, execution_params)
                quote_attempts.append(attempt_evidence(pricing, quote_fetch_started_at, quote_received_at, quote_fetch_seconds))
            except Exception as exc:
                quote_attempts.append({"quote_timestamp_status": "unavailable", "error": type(exc).__name__})
            guarded = guarded_plan()
            if guarded is not None:
                return guarded
        if pricing.approved:
            _context["pricing_references"].setdefault(selection.option_symbol, {
                "entry_original_mid": pricing.original_mid, "entry_price_ceiling": pricing.max_entry_price,
                "entry_wide_market": pricing.wide_market,
                "entry_starting_bid": quote.bid, "entry_starting_ask": quote.ask,
                "entry_starting_quote_at": pricing.evidence().get("effective_quote_at")})
        pricing_evidence = pricing.evidence()
        first_provider_at = parse_provider_timestamp(quote_attempts[0].get("effective_quote_at"))
        final_provider_at = parse_provider_timestamp(quote_attempts[-1].get("effective_quote_at"))
        pricing_evidence = {
            **pricing_evidence,
            "initial_mid": pricing.original_mid,
            "starting_bid": execution_params.get("entry_starting_bid", quote.bid),
            "starting_ask": execution_params.get("entry_starting_ask", quote.ask),
            "starting_quote_at": execution_params.get("entry_starting_quote_at", pricing_evidence.get("effective_quote_at")),
            "entry_execution_profile": active_entry_profile.name if active_entry_profile is not None else "legacy",
            "entry_reprice_max_chase_pct": resolve_entry_reprice_max_chase_pct(
                deployment.execution.model_dump()
            ),
            "initial_limit_price": pricing.limit_price,
            "quote_attempts": quote_attempts,
            "quote_refresh_status": (
                "recovered" if refreshed_quote and pricing.approved else
                "unavailable" if refreshed_quote else "not_needed"
            ),
            "provider_timestamp_advanced": (
                final_provider_at > first_provider_at
                if len(quote_attempts) == 2 and first_provider_at is not None and final_provider_at is not None else None
            ),
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

        base_max_trade_premium = (
            deployment.risk.max_trade_premium_usd or 300.0
        )
        max_trade_premium = base_max_trade_premium
        cap_fraction: float | None = None
        if deployment.exit.risk_envelope_live_mode == "canary":
            configured_cap_fraction = (
                deployment.exit.risk_envelope_live_max_premium_cap_fraction
            )
            if configured_cap_fraction is None:
                raise ValueError(
                    "armed risk-envelope canary is missing its premium cap"
                )
            cap_fraction = float(configured_cap_fraction)
            max_trade_premium = base_max_trade_premium * cap_fraction
        premium_cap_receipt = {
            "base_max_trade_premium_usd": base_max_trade_premium,
            "risk_envelope_cap_fraction": cap_fraction,
            "effective_max_trade_premium_usd": max_trade_premium,
        }
        sizing_price = float(pricing.max_entry_price or entry_price)
        min_contract_cost = sizing_price * 100
        quantity = int(max_trade_premium // (sizing_price * 100))
        if deployment.risk.max_contracts is not None:
            quantity = min(quantity, int(deployment.risk.max_contracts))
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
                    **premium_cap_receipt,
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
            proposed_trade_premium_usd=sizing_price * quantity * 100,
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
                risk_details={
                    "entry_pricing": pricing_evidence,
                    **premium_cap_receipt,
                    **selection_details,
                },
            )

        guarded = guarded_plan(check_freshness=True)
        if guarded is not None:
            return guarded
        if simulate_only:
            try:
                checked = await self.order_manager.preflight_entry(selection.option_symbol, entry_price, quantity)
                if not checked.current_increment:
                    raise ValueError("public_preflight_tick_metadata_unavailable")
                normalized = float(checked.payload["limitPrice"])
                if normalized > entry_price + 1e-9:
                    raise ValueError("broker_entry_price_exceeds_limit")
                entry_price = normalized
                pricing_evidence.update({"initial_limit_price": entry_price,
                    "preflight_limit_price": entry_price, "preflight_increment": checked.current_increment,
                    "tick_validation_basis": "broker_preflight",
                    "tick_metadata_status": "proved" if checked.current_increment else "unavailable",
                    "sizing_price": sizing_price, "modeled_fill_basis": "later_fresh_ask_touch"})
            except Exception as exc:
                reason = "public_preflight_tick_metadata_unavailable" if str(exc) == "public_preflight_tick_metadata_unavailable" else "public_preflight_failed"
                pricing_evidence["tick_metadata_status"] = "unavailable"
                return TradePlan(trade_id=trade_id, deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol, direction=decision.direction, option_symbol=selection.option_symbol,
                    quantity=quantity, estimated_entry_price=entry_price, risk_reasons=[reason],
                    dry_run=True, order_id=None, underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp, risk_details={"entry_pricing": pricing_evidence,
                        "pre_submission_retryable": preflight_failure_is_transient(exc),
                        "error_type": type(exc).__name__, **premium_cap_receipt, **selection_details})
            guarded = guarded_plan(check_freshness=True)
            if guarded is not None:
                return guarded
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
                    risk_details={
                        "entry_pricing": pricing_evidence,
                        **premium_cap_receipt,
                        **selection_details,
                    },
                    execution_route=route,
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
                risk_details={
                    "entry_pricing": pricing_evidence,
                    **premium_cap_receipt,
                    **selection_details,
                },
                execution_route=route,
            )

        sizing_receipts = []
        stage = "entry_sizing"
        try:
            quantity, sizing_reason, receipt = await self._fit_live_quantity(
                trade_id=trade_id, deployment=deployment, timestamp=decision.timestamp,
                price=sizing_price, upper_quantity=quantity, premium_cap=max_trade_premium)
            sizing_receipts.append(receipt)
            if sizing_reason:
                raise _SizingBlocked(sizing_reason)
            # A normalized broker price or fixed fees can reduce capacity. Re-preflight
            # the actual smaller quantity; never approve by scaling old broker costs.
            for attempt in range(3):
                stage = "public_preflight"
                started = clock.monotonic()
                preflight = await self.order_manager.preflight_entry(selection.option_symbol, entry_price, quantity)
                pricing_evidence.setdefault("preflight_attempts", []).append({"quantity": quantity,
                    "price": float(preflight.payload["limitPrice"]), "latency_seconds": clock.monotonic()-started})
                stage = "entry_sizing"
                normalized_price = float(preflight.payload["limitPrice"])
                required = max(preflight.buying_power_requirement or 0.0,
                               preflight.estimated_cost or 0.0, normalized_price * quantity * 100)
                fitted, sizing_reason, receipt = await self._fit_live_quantity(
                    trade_id=trade_id, deployment=deployment, timestamp=decision.timestamp,
                    price=max(sizing_price, normalized_price), upper_quantity=quantity, premium_cap=max_trade_premium,
                    extra_cash=max(0.0, required - normalized_price * quantity * 100))
                sizing_receipts.append(receipt)
                if sizing_reason:
                    quantity = fitted
                    raise _SizingBlocked(sizing_reason)
                if fitted == quantity:
                    break
                quantity = fitted
            else:
                raise _SizingBlocked("entry_sizing_preflight_unstable")
            pricing_evidence["entry_sizing"] = sizing_receipts
        except Exception as exc:
            return TradePlan(
                trade_id=trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                direction=decision.direction,
                option_symbol=selection.option_symbol,
                quantity=quantity,
                estimated_entry_price=entry_price,
                risk_reasons=[str(exc) if isinstance(exc, _SizingBlocked) else f"{stage}_failed:{exc}"],
                dry_run=False,
                order_id=None,
                underlying_entry_price=underlying_entry_price,
                entry_timestamp=decision.timestamp,
                risk_details={
                    "entry_pricing": pricing_evidence,
                    "entry_sizing": sizing_receipts,
                    "pre_submission_retryable": stage == "public_preflight" and preflight_failure_is_transient(exc),
                    **premium_cap_receipt,
                    **selection_details,
                },
            )

        final_limit_price = float(preflight.payload["limitPrice"])
        if final_limit_price > entry_price + 1e-9:
            return TradePlan(
                trade_id=trade_id, deployment_id=deployment.deployment_id, symbol=deployment.symbol,
                direction=decision.direction, option_symbol=selection.option_symbol, quantity=quantity,
                estimated_entry_price=entry_price, risk_reasons=["broker_entry_price_exceeds_limit"], dry_run=False,
                entry_timestamp=decision.timestamp, risk_details={"entry_pricing": pricing_evidence, **selection_details},
            )
        pricing_evidence = {
            **pricing_evidence,
            "preflight_limit_price": final_limit_price,
            "initial_limit_price": final_limit_price,
            "preflight_increment": preflight.current_increment,
            "tick_metadata_status": "proved" if preflight.current_increment else "unavailable",
            "tick_validation_basis": "broker_preflight",
            "sizing_price": sizing_price,
            "preflight_buying_power_requirement": preflight.buying_power_requirement,
            "preflight_estimated_cost": preflight.estimated_cost,
        }
        required_cash = max(
            preflight.buying_power_requirement or 0.0,
            preflight.estimated_cost or 0.0,
            sizing_price * quantity * 100 + max(0.0,
                (preflight.estimated_cost or 0.0) - final_limit_price * quantity * 100),
        )
        sized_risk_details: dict[str, object] = {}
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
                        **premium_cap_receipt,
                        **selection_details,
                        **sized_risk_details,
                        **cash_guard_details,
                    },
                )
        entry_intent = TradeRecord(trade_id=trade_id,
                    deployment_id=deployment.deployment_id, symbol=deployment.symbol,
                    option_symbol=selection.option_symbol, quantity=quantity, entry_price=final_limit_price,
                    underlying_entry_price=underlying_entry_price, entry_timestamp=decision.timestamp,
                    entry_order_id=trade_id, status="pending_entry_reconcile")
        async def clear_unsubmitted_intent():
            if self.entry_intent_repository is not None:
                await self.entry_intent_repository.upsert_trade(replace(entry_intent,
                    quantity=0, entry_price=None, entry_order_id=None, status="closed"))
        if self.entry_intent_repository is not None:
            try:
                await self.entry_intent_repository.upsert_trade(entry_intent)
            except Exception:
                if self.cash_guard is not None:
                    await self.cash_guard.release_entry(trade_id)
                if self.risk_manager is not None:
                    await self.risk_manager.release_sized_entry(trade_id)
                return TradePlan(trade_id=trade_id, deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol, direction=decision.direction, option_symbol=selection.option_symbol,
                    quantity=quantity, estimated_entry_price=final_limit_price,
                    risk_reasons=["entry_intent_persistence_failed"], dry_run=False, order_id=None,
                    entry_timestamp=decision.timestamp, risk_details={"entry_pricing": pricing_evidence})
        # Cash reservation can await broker/account state.  It must therefore
        # happen before the final sized-risk/canary reservation.  Once the
        # final reservation returns, only quote/permission checks precede broker
        # submission: a latch or expiry observed while cash was being
        # reserved cannot slip through on a stale earlier approval.
        if self.risk_manager is not None:
            stop_loss_pct, stop_loss_source = resolve_planned_stop_loss_pct(deployment)
            try:
                sized_risk = await self.risk_manager.reserve_sized_entry(
                    trade_id=trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    entry_price=sizing_price,
                    quantity=quantity,
                    stop_loss_pct=stop_loss_pct,
                )
            except Exception as exc:
                if self.cash_guard is not None:
                    await self.cash_guard.release_entry(trade_id)
                # The risk operation may have persisted its reservation before
                # failing during a later receipt/event write. Clean it up
                # idempotently before returning a fail-closed plan.
                await self.risk_manager.release_sized_entry(trade_id)
                await clear_unsubmitted_intent()
                return TradePlan(
                    trade_id=trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol=selection.option_symbol,
                    quantity=quantity,
                    estimated_entry_price=final_limit_price,
                    risk_reasons=[f"sized_entry_risk_check_failed:{exc}"],
                    dry_run=False,
                    order_id=None,
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp,
                    risk_details={
                        "required_cash": required_cash,
                        "buying_power_requirement": preflight.buying_power_requirement,
                        "estimated_cost": preflight.estimated_cost,
                        "entry_pricing": pricing_evidence,
                        **premium_cap_receipt,
                        **selection_details,
                        **cash_guard_details,
                    },
                )
            sized_risk_details = {
                "sized_entry_risk": dict(sized_risk.details),
                "planned_stop_loss_pct": stop_loss_pct,
                "planned_stop_loss_source": stop_loss_source,
            }
            if not sized_risk.allowed:
                if self.cash_guard is not None:
                    await self.cash_guard.release_entry(trade_id)
                await clear_unsubmitted_intent()
                return TradePlan(
                    trade_id=trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol=selection.option_symbol,
                    quantity=quantity,
                    estimated_entry_price=final_limit_price,
                    risk_reasons=[sized_risk.reason or "sized_entry_risk_blocked"],
                    dry_run=False,
                    order_id=None,
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=decision.timestamp,
                    risk_details={
                        "required_cash": required_cash,
                        "buying_power_requirement": preflight.buying_power_requirement,
                        "estimated_cost": preflight.estimated_cost,
                        "entry_pricing": pricing_evidence,
                        **premium_cap_receipt,
                        **selection_details,
                        **cash_guard_details,
                        **sized_risk_details,
                    },
                )
        # Recheck a queued entry intent after all awaited selection/preflight/
        # reservation work and immediately before broker submission.
        guarded = guarded_plan(check_freshness=True, before_submission=True)
        if guarded is not None:
            if self.cash_guard is not None:
                await self.cash_guard.release_entry(trade_id)
            if self.risk_manager is not None:
                await self.risk_manager.release_sized_entry(trade_id)
            await clear_unsubmitted_intent()
            return guarded
        def submission_guard():
            blocked = guarded_plan(check_freshness=True, before_submission=True)
            return blocked.risk_reasons[0] if blocked else None
        try:
            result: OrderResult = await self.order_manager.place_entry_order(
                selection.option_symbol, final_limit_price, quantity, order_id=trade_id,
                **({"submission_guard": submission_guard} if isinstance(self.order_manager, OrderManager) else {}),
            )
        except Exception as exc:
            result = OrderResult(order_id=trade_id, error=f"entry_submission_uncertain:{type(exc).__name__}",
                                 submission_uncertain=True, actual_limit_price=final_limit_price)
        if self.entry_intent_repository is not None and result.order_id is None:
            await clear_unsubmitted_intent()
        if self.cash_guard is not None and (result.order_id is None or (result.error and not getattr(result, "submission_uncertain", False))):
            await self.cash_guard.release_entry(trade_id)
        if self.risk_manager is not None and (result.order_id is None or (result.error and not getattr(result, "submission_uncertain", False))):
            await self.risk_manager.release_sized_entry(trade_id)
        pricing_evidence["submitted_limit_price"] = getattr(result, "actual_limit_price", None)
        if getattr(result, "actual_limit_price", None) is not None:
            final_limit_price = result.actual_limit_price
        pricing_evidence["submission_uncertain"] = getattr(result, "submission_uncertain", False)
        pricing_evidence["submission_error"] = result.error
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
                "pre_submission_retryable": getattr(result, "pre_submission_retryable", False),
                "required_cash": required_cash,
                "buying_power_requirement": preflight.buying_power_requirement,
                "estimated_cost": preflight.estimated_cost,
                "entry_pricing": pricing_evidence,
                **premium_cap_receipt,
                **selection_details,
                **sized_risk_details,
                **cash_guard_details,
            },
            execution_route=route,
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
    ) -> tuple[object | None, bool]:
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
        except Exception:
            # A snapshot failure must never break (or even flag) the entry
            # path -- the trade matters more than the telemetry.
            return None, False
        try:
            await self.chain_snapshot_repository.record_attempt(attempt)
        except Exception:
            return attempt, False
        return attempt, True


def _entry_window_allows(deployment: DeploymentManifest, timestamp) -> bool:
    start = _parse_optional_et_time(deployment.execution.entry_window_start_et)
    end = _parse_optional_et_time(deployment.execution.entry_window_end_et)
    current = as_et_time(timestamp)
    if deployment.exit.management_exit and deployment.exit.eod_flat:
        flat = _parse_optional_et_time(deployment.exit.hard_flat_time_et)
        if flat is not None and current >= flat:
            return False
    if start is None and end is None:
        return True
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
    selected_spread_pct = None
    if selection.bid is not None and selection.ask is not None:
        mid = (float(selection.bid) + float(selection.ask)) / 2
        if mid > 0:
            selected_spread_pct = abs(float(selection.ask) - float(selection.bid)) / mid
    details = {
        "selected_open_interest": selection.open_interest,
        "open_interest_percentile": selection.open_interest_percentile,
        "selected_dte": selection.dte,
        "selected_abs_delta": selection.abs_delta,
        "selected_bid": selection.bid,
        "selected_ask": selection.ask,
        "selected_spread_pct": selected_spread_pct,
    }
    if selection.dte_fallback_policy is not None:
        details.update(
            {
                "dte_fallback_policy": selection.dte_fallback_policy,
                "requested_dte_min": selection.requested_dte_min,
                "requested_dte_max": selection.requested_dte_max,
                "attempted_fallback_dtes_count": selection.attempted_fallback_dtes_count,
            }
        )
    return details


def _selection_snapshot_details(attempt, *, persisted: bool) -> dict:
    if attempt is None:
        return {
            "option_selection_snapshot_id": None,
            "option_selection_snapshot_persisted": False,
            "option_candidate_set_sha256": None,
            "actual_option_selection_sha256": None,
        }
    return {
        "option_selection_snapshot_id": attempt.snapshot_id,
        "option_selection_snapshot_persisted": persisted,
        "option_candidate_set_sha256": attempt.option_candidate_set_sha256,
        "actual_option_selection_sha256": attempt.actual_option_selection_sha256,
    }


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
