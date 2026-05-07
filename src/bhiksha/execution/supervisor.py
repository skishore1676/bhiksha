"""Execution supervision for planned trades."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import UTC
import math
import uuid
from typing import Any

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.config.models import AppConfig
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import ExitMode, SignalDirection
from bhiksha.domain.models import ExitDecision, ExitPlan, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.execution.order_manager import OrderResult, normalize_option_symbol, round_price
from bhiksha.integrations.manual_sheet_status import ManualSheetStatusWriter
from bhiksha.market_data.session import as_et_time
from bhiksha.persistence.repository import EventRepository, NullEventRepository, NullTradeStateRepository, TradeStateRepository
from bhiksha.state.lifecycle import LifecycleTransition, TradeLifecycleStore
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.time_utils import parse_time_text


class ExecutionSupervisor:
    """Coordinates planning and event logging for a signal."""

    def __init__(
        self,
        planner: ExecutionPlanner | None = None,
        event_repository: EventRepository | None = None,
        app_config: AppConfig | None = None,
        lifecycle_store: TradeLifecycleStore | None = None,
        event_bus: InMemoryEventBus | None = None,
        trade_state_repository: TradeStateRepository | None = None,
        manual_status_writer: ManualSheetStatusWriter | None = None,
        reconcile_trigger: asyncio.Event | None = None,
    ) -> None:
        self.planner = planner or ExecutionPlanner()
        self.event_repository = event_repository or NullEventRepository()
        self.trade_state_repository = trade_state_repository or NullTradeStateRepository()
        self.app_config = app_config or AppConfig()
        self.lifecycle_store = lifecycle_store or TradeLifecycleStore()
        self.event_bus = event_bus
        self.manual_status_writer = manual_status_writer
        self.reconcile_trigger = reconcile_trigger
        self._entry_lock = asyncio.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._disabled_entry_deployments: set[str] = set()

    async def close(self) -> None:
        await self.planner.close()

    async def handle_signal(
        self,
        deployment: DeploymentManifest,
        decision: SignalDecision,
        *,
        dry_run: bool,
        simulate_only: bool = False,
        live_entry_block_reason: str | None = None,
    ) -> TradePlan | None:
        await self.event_repository.append(
            "signal_decision",
            {
                "deployment_id": decision.deployment_id,
                "symbol": decision.symbol,
                "timestamp": decision.timestamp.isoformat(),
                "signal": decision.signal,
                "direction": decision.direction.value if decision.direction else None,
                "reason": decision.reason,
                "features": decision.features,
            },
        )
        if not self.can_submit_deployment_entry(deployment):
            return None
        if decision.signal:
            if _is_self_disarming_manual_deployment(deployment):
                self._disabled_entry_deployments.add(deployment.deployment_id)
            await self._record_manual_status(
                deployment,
                stage="signal_triggered",
                writer_call=self.manual_status_writer.mark_signal_triggered(deployment, decision)
                if self.manual_status_writer is not None
                else None,
            )
        if self.event_bus is not None:
            await self.event_bus.publish(SignalEvaluatedEvent(decision=decision))
        async with self._entry_lock:
            lifecycle = self.lifecycle_store.get(deployment.symbol, deployment.deployment_id)
            if not self.lifecycle_store.can_submit_entry(deployment.symbol, deployment.deployment_id):
                await self.event_repository.append(
                    "lifecycle_entry_blocked",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": deployment.symbol,
                        "state": lifecycle.state.value if lifecycle else None,
                    },
                )
                await self._record_manual_status(
                    deployment,
                    stage="entry_blocked",
                    writer_call=self.manual_status_writer.mark_entry_blocked(
                        deployment,
                        event_at=decision.timestamp,
                        note=f"lifecycle_blocked:{lifecycle.state.value if lifecycle else 'unknown'}",
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
                return None
            if decision.signal and decision.direction is not None and live_entry_block_reason and not dry_run and not simulate_only:
                plan = TradePlan(
                    trade_id=str(uuid.uuid4()),
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    direction=decision.direction,
                    option_symbol="",
                    quantity=0,
                    estimated_entry_price=0.0,
                    risk_reasons=[live_entry_block_reason],
                    dry_run=False,
                    order_id=None,
                    underlying_entry_price=_underlying_entry_price(decision),
                    entry_timestamp=decision.timestamp,
                )
            else:
                plan = await self.planner.plan_entry(
                    deployment,
                    decision,
                    dry_run=dry_run,
                    simulate_only=simulate_only,
                )
            if plan is not None:
                if (
                    _entry_plan_approved(plan)
                    and plan.quantity > 0
                    and plan.option_symbol
                    and (plan.order_id is not None or plan.dry_run)
                ):
                    mode = "live" if plan.order_id and not plan.dry_run else ("shadow" if simulate_only else "dry_run")
                    await self._record_manual_status(
                        deployment,
                        stage="entry_planned",
                        writer_call=self.manual_status_writer.mark_entry_planned(
                            deployment,
                            plan=plan,
                            mode=mode,
                        )
                        if self.manual_status_writer is not None
                        else None,
                    )
                else:
                    note = ",".join(plan.risk_reasons) or "entry_blocked"
                    await self._record_manual_status(
                        deployment,
                        stage="entry_blocked",
                        writer_call=self.manual_status_writer.mark_entry_blocked(
                            deployment,
                            event_at=plan.entry_timestamp or decision.timestamp,
                            note=note,
                            trade_id=plan.trade_id,
                        )
                        if self.manual_status_writer is not None
                        else None,
                    )
            if plan is not None:
                if plan.order_id:
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="pending_entry",
                            entry_order_id=plan.order_id,
                        )
                    )
                    transition = self.lifecycle_store.begin_entry(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id=plan.order_id,
                    )
                    await self._emit_lifecycle_transition(transition, reason="entry_submitted")
                if (
                    simulate_only
                    and _entry_plan_approved(plan)
                    and plan.quantity > 0
                    and plan.option_symbol
                    and plan.order_id is None
                ):
                    self.planner.position_tracker.open_position(
                        deployment.symbol,
                        deployment.deployment_id,
                        trade_id=plan.trade_id,
                        option_symbol=plan.option_symbol,
                        quantity=plan.quantity,
                        entry_price=plan.estimated_entry_price,
                        underlying_entry_price=plan.underlying_entry_price,
                        entry_timestamp=plan.entry_timestamp,
                        source="shadow",
                        order_id="SHADOW_ENTRY",
                    )
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="open_unprotected",
                            entry_order_id="SHADOW_ENTRY",
                        )
                    )
                    transition = self.lifecycle_store.mark_open(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id="SHADOW_ENTRY",
                        protected=False,
                    )
                    await self._emit_lifecycle_transition(transition, reason="shadow_entry_open")
                    await self.event_repository.append(
                        "shadow_entry_assumed",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": deployment.symbol,
                            "trade_id": plan.trade_id,
                            "option_symbol": plan.option_symbol,
                            "quantity": plan.quantity,
                            "entry_price": plan.estimated_entry_price,
                            "underlying_entry_price": plan.underlying_entry_price,
                            "entry_timestamp": plan.entry_timestamp.isoformat() if plan.entry_timestamp else None,
                            "risk_reasons": list(plan.risk_reasons),
                        },
                    )
                elif not dry_run and plan.order_id:
                    plan = await self._protect_live_entry(plan, deployment)
                elif dry_run and plan.order_id:
                    await self._upsert_trade_record(
                        TradeRecord(
                            trade_id=plan.trade_id,
                            deployment_id=deployment.deployment_id,
                            symbol=deployment.symbol,
                            option_symbol=plan.option_symbol,
                            quantity=plan.quantity,
                            entry_price=plan.estimated_entry_price,
                            underlying_entry_price=plan.underlying_entry_price,
                            entry_timestamp=plan.entry_timestamp,
                            status="open_unprotected",
                            entry_order_id=plan.order_id,
                        )
                    )
                    transition = self.lifecycle_store.mark_open(
                        deployment.symbol,
                        deployment.deployment_id,
                        option_symbol=plan.option_symbol,
                        order_id=plan.order_id,
                        protected=False,
                    )
                    await self._emit_lifecycle_transition(transition, reason="dry_run_entry_open")
                await self.event_repository.append("trade_plan", asdict(plan))
            return plan

    def can_submit_deployment_entry(self, deployment: DeploymentManifest) -> bool:
        if not deployment.enabled:
            return False
        if _is_self_disarming_manual_deployment(deployment):
            return deployment.deployment_id not in self._disabled_entry_deployments
        return True

    async def _protect_live_entry(self, plan: TradePlan, deployment: DeploymentManifest) -> TradePlan:
        filled, payload, error = await self.planner.order_manager.wait_for_fill(
            plan.order_id,
            timeout_seconds=self.app_config.order_fill_timeout_seconds,
            poll_seconds=self.app_config.order_fill_poll_seconds,
        )
        await self.event_repository.append(
            "entry_fill_check",
            {
                "deployment_id": plan.deployment_id,
                "order_id": plan.order_id,
                "filled": filled,
                "error": error,
                "payload": payload or {},
            },
        )
        if not filled:
            normalized_error = (error or "").upper()
            if normalized_error in {"REJECTED", "CANCELED", "EXPIRED"}:
                await self._release_cash_guard_reservation(plan.trade_id)
                self.planner.position_tracker.close_position(
                    deployment.symbol,
                    deployment.deployment_id,
                    option_symbol=plan.option_symbol,
                    order_id=plan.order_id,
                )
                await self.trade_state_repository.mark_closed(plan.trade_id)
                transition = self.lifecycle_store.mark_closed(deployment.symbol, deployment.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="entry_unfilled_closed")
                await self.event_repository.append(
                    "entry_reconcile_released",
                    {
                        "deployment_id": plan.deployment_id,
                        "trade_id": plan.trade_id,
                        "order_id": plan.order_id,
                        "status": normalized_error or "UNKNOWN",
                        "payload": payload or {},
                    },
                )
                return plan
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=plan.trade_id,
                    deployment_id=deployment.deployment_id,
                    symbol=deployment.symbol,
                    option_symbol=plan.option_symbol,
                    quantity=plan.quantity,
                    entry_price=plan.estimated_entry_price,
                    underlying_entry_price=plan.underlying_entry_price,
                    entry_timestamp=plan.entry_timestamp,
                    status="pending_entry_reconcile",
                    entry_order_id=plan.order_id,
                )
            )
            transition = self.lifecycle_store.mark_reconciliation_hold(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=plan.order_id,
            )
            await self._emit_lifecycle_transition(transition, reason="entry_fill_timeout_reconcile")
            await self.event_repository.append(
                "entry_fill_timeout_reconcile",
                {
                    "deployment_id": plan.deployment_id,
                    "trade_id": plan.trade_id,
                    "order_id": plan.order_id,
                    "error": error,
                    "payload": payload or {},
                },
            )
            if self.reconcile_trigger is not None:
                self.reconcile_trigger.set()
            return plan

        await self._finalize_cash_guard_reservation(plan.trade_id)
        stop_result, stop_price, target_order_id, target_price = await self._arm_position_protection(
            deployment,
            option_symbol=plan.option_symbol,
            quantity=plan.quantity,
            entry_price=plan.estimated_entry_price,
            dry_run=False,
            event_payload={
                "deployment_id": plan.deployment_id,
                "entry_order_id": plan.order_id,
            },
        )
        self.planner.position_tracker.open_position(
            deployment.symbol,
            deployment.deployment_id,
            trade_id=plan.trade_id,
            option_symbol=plan.option_symbol,
            quantity=plan.quantity,
            entry_price=plan.estimated_entry_price,
            underlying_entry_price=plan.underlying_entry_price,
            entry_timestamp=plan.entry_timestamp,
            source="live_open",
            order_id=plan.order_id,
            stop_order_id=stop_result.order_id,
            stop_price=stop_price,
            target_order_id=target_order_id,
            target_price=target_price,
        )
        await self._upsert_trade_record(
            TradeRecord(
                trade_id=plan.trade_id,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                option_symbol=plan.option_symbol,
                quantity=plan.quantity,
                entry_price=plan.estimated_entry_price,
                underlying_entry_price=plan.underlying_entry_price,
                entry_timestamp=plan.entry_timestamp,
                status="target_active" if target_order_id else "open_protected",
                entry_order_id=plan.order_id,
                stop_order_id=stop_result.order_id,
                stop_price=stop_price,
                target_order_id=target_order_id,
                target_price=target_price,
            )
        )
        if target_order_id:
            transition = self.lifecycle_store.mark_target_active(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=target_order_id,
            )
            await self._emit_lifecycle_transition(transition, reason="entry_filled_target_active")
        else:
            transition = self.lifecycle_store.mark_open(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=stop_result.order_id or plan.order_id,
                protected=bool(stop_result.order_id),
            )
            await self._emit_lifecycle_transition(transition, reason="entry_filled_open_protected")
        return replace(plan, stop_order_id=stop_result.order_id, target_order_id=target_order_id)

    async def manage_open_position(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition | None:
        if position.source == "shadow" or deployment.execution.shadow_only:
            dry_run = True
        async with self._symbol_locks[position.symbol]:
            return await self._manage_open_position_locked(deployment, position, dry_run=dry_run)

    async def _manage_open_position_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition | None:
        if position.option_symbol is None or position.quantity <= 0:
            return None
        if position.entry_price is None:
            return None
        if position.exit_mode is not None or position.exit_order_id is not None:
            return position

        updated = position
        quote = None

        if updated.stop_order_id is None and updated.target_order_id is None:
            updated = await self._restore_missing_protection(deployment, updated, dry_run=dry_run)

        async def ensure_quote():
            nonlocal quote
            if quote is None:
                quote = await self.planner.order_manager.get_option_quote(updated.option_symbol)
            return quote
        if dry_run and updated.source == "shadow":
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            await self.event_repository.append(
                "shadow_mark",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": updated.symbol,
                    "trade_id": updated.trade_id,
                    "option_symbol": updated.option_symbol,
                    "quantity": updated.quantity,
                    "entry_price": updated.entry_price,
                    "mark_price": reference_price,
                    "bid": current_quote.bid,
                    "ask": current_quote.ask,
                    "last": current_quote.last,
                    "spread_pct": current_quote.spread_pct,
                    "unrealized_pnl_usd": _premium_pnl(updated.entry_price, reference_price, updated.quantity),
                    "unrealized_stop_r": _realized_stop_r(
                        updated.entry_price,
                        reference_price,
                        deployment.exit.stop_loss_pct or deployment.risk.stop_loss_pct,
                    ),
                },
            )
        if (
            _profit_target_configured(deployment)
            and updated.target_order_id is None
            and updated.target_price is None
        ):
            target_price = _deployment_target_price(deployment, position.entry_price)
            if self._supports_concurrent_exit_orders():
                target_order_id = "DRY_RUN_TARGET"
                target_error = None
                if not dry_run:
                    result = await self.planner.order_manager.place_target_order(position.option_symbol, target_price, position.quantity)
                    target_order_id = result.order_id
                    target_error = result.error
                await self.event_repository.append(
                    "profit_target_submission",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "target_order_id": target_order_id,
                        "target_error": target_error,
                        "target_price": target_price,
                        "source": "position_manager",
                    },
                )
                updated = _replace_position(updated, target_order_id=target_order_id, target_price=target_price)
            else:
                await self.event_repository.append(
                    "profit_target_armed",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "target_order_id": None,
                        "target_price": target_price,
                        "mode": "virtual",
                        "reason": "single_resting_exit_order_broker",
                        "source": "position_manager",
                    },
                )
                updated = _replace_position(updated, target_order_id=None, target_price=target_price)

        if (
            not self._supports_concurrent_exit_orders()
            and updated.target_price is not None
            and deployment.exit.target_approach_offset_pct is not None
            and updated.target_order_id is None
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            activation_price = updated.target_price * (1.0 - deployment.exit.target_approach_offset_pct)
            if reference_price is not None and reference_price >= activation_price:
                cancel_ok = True
                cancel_error = None
                canceled_stop_order_id = updated.stop_order_id
                if updated.stop_order_id and not dry_run:
                    cancel_ok, cancel_error = await self.planner.order_manager.cancel_order(updated.stop_order_id)
                    await self.event_repository.append(
                        "protection_cancel_attempt",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "stop_order_id": updated.stop_order_id,
                            "canceled": cancel_ok,
                            "error": cancel_error,
                            "reason": "virtual_target_activation",
                        },
                    )
                can_submit_target = dry_run or cancel_ok or self._allows_exit_submission_before_cancel_confirmation()
                if can_submit_target:
                    target_order_id = "DRY_RUN_TARGET_ACTIVATED"
                    target_error = cancel_error
                    if not dry_run:
                        result = await self.planner.order_manager.place_target_order(
                            updated.option_symbol,
                            updated.target_price,
                            updated.quantity,
                        )
                        target_order_id = result.order_id
                        target_error = result.error or target_error
                    await self.event_repository.append(
                        "virtual_target_activation",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "reference_price": reference_price,
                            "activation_price": activation_price,
                            "target_price": updated.target_price,
                            "canceled_stop_order_id": canceled_stop_order_id,
                            "target_order_id": target_order_id,
                            "target_error": target_error,
                        },
                    )
                    if target_order_id is not None:
                        updated = _replace_position(
                            updated,
                            stop_order_id=None,
                            target_order_id=target_order_id,
                        )
                        transition = self.lifecycle_store.mark_target_active(
                            updated.symbol,
                            updated.deployment_id,
                            option_symbol=updated.option_symbol,
                            order_id=target_order_id,
                        )
                        await self._emit_lifecycle_transition(transition, reason="virtual_target_activation")

        if (
            not self._supports_concurrent_exit_orders()
            and updated.target_order_id is not None
            and updated.target_price is not None
            and deployment.exit.target_pullback_restore_progress_pct is not None
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            restore_threshold = _restore_threshold_price(
                updated.entry_price,
                updated.target_price,
                deployment.exit.target_pullback_restore_progress_pct,
            )
            if reference_price is not None and reference_price <= restore_threshold:
                cancel_ok = True
                cancel_error = None
                canceled_target_order_id = updated.target_order_id
                if updated.target_order_id and not dry_run:
                    cancel_ok, cancel_error = await self.planner.order_manager.cancel_order(updated.target_order_id)
                    await self.event_repository.append(
                        "target_cancel_attempt",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "target_order_id": updated.target_order_id,
                            "canceled": cancel_ok,
                            "error": cancel_error,
                            "reason": "virtual_target_pullback_restore",
                        },
                    )
                can_restore_stop = dry_run or cancel_ok or self._allows_exit_submission_before_cancel_confirmation()
                if can_restore_stop:
                    restored_stop_price = updated.stop_price or (updated.entry_price * (1.0 - deployment.exit.stop_loss_pct))
                    stop_order_id = "DRY_RUN_RESTORED_STOP"
                    stop_error = cancel_error
                    if not dry_run:
                        result = await self.planner.order_manager.place_stop_loss_order(
                            updated.option_symbol,
                            restored_stop_price,
                            updated.quantity,
                        )
                        stop_order_id = result.order_id
                        stop_error = result.error or stop_error
                    await self.event_repository.append(
                        "virtual_target_pullback_restore",
                        {
                            "deployment_id": deployment.deployment_id,
                            "symbol": updated.symbol,
                            "option_symbol": updated.option_symbol,
                            "reference_price": reference_price,
                            "restore_threshold": restore_threshold,
                            "canceled_target_order_id": canceled_target_order_id,
                            "restored_stop_order_id": stop_order_id,
                            "restored_stop_price": restored_stop_price,
                            "stop_error": stop_error,
                        },
                    )
                    if stop_order_id is not None:
                        updated = _replace_position(
                            updated,
                            stop_order_id=stop_order_id,
                            stop_price=restored_stop_price,
                            target_order_id=None,
                        )
                        transition = self.lifecycle_store.mark_open(
                            updated.symbol,
                            updated.deployment_id,
                            option_symbol=updated.option_symbol,
                            order_id=stop_order_id,
                            protected=True,
                        )
                        await self._emit_lifecycle_transition(transition, reason="virtual_target_pullback_restore")

        if (
            deployment.exit.stop_to_breakeven_after_r_multiple is not None
            and (updated.stop_price is None or updated.stop_price + 1e-9 < updated.entry_price)
        ):
            current_quote = await ensure_quote()
            reference_price = current_quote.exit_reference_price
            trigger_price = _target_price(
                updated.entry_price,
                deployment.exit.stop_loss_pct,
                deployment.exit.stop_to_breakeven_after_r_multiple,
            )
            if reference_price is not None and reference_price >= trigger_price:
                canceled_stop_order_id = updated.stop_order_id
                cancel_error = None
                if updated.stop_order_id and not dry_run:
                    canceled, cancel_error = await self.planner.order_manager.cancel_order(updated.stop_order_id)
                    if not canceled:
                        await self.event_repository.append(
                            "protection_cancel_attempt",
                            {
                                "deployment_id": deployment.deployment_id,
                                "symbol": updated.symbol,
                                "option_symbol": updated.option_symbol,
                                "stop_order_id": updated.stop_order_id,
                                "canceled": canceled,
                                "error": cancel_error,
                            },
                        )
                        return updated
                new_stop_order_id = "DRY_RUN_BREAKEVEN_STOP"
                new_stop_error = cancel_error
                if not dry_run:
                    result = await self.planner.order_manager.place_stop_loss_order(
                        updated.option_symbol,
                        updated.entry_price,
                        updated.quantity,
                    )
                    new_stop_order_id = result.order_id
                    new_stop_error = result.error
                await self.event_repository.append(
                    "breakeven_stop_promotion",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": updated.symbol,
                        "option_symbol": updated.option_symbol,
                        "reference_price": reference_price,
                        "trigger_price": trigger_price,
                        "canceled_stop_order_id": canceled_stop_order_id,
                        "new_stop_order_id": new_stop_order_id,
                        "new_stop_error": new_stop_error,
                        "new_stop_price": updated.entry_price,
                    },
                )
                updated = _replace_position(
                    updated,
                    stop_order_id=new_stop_order_id,
                    stop_price=updated.entry_price,
                )
                transition = self.lifecycle_store.mark_open(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=new_stop_order_id,
                    protected=True,
                )
                await self._emit_lifecycle_transition(transition, reason="breakeven_stop_promotion")

        if updated != position:
            self.planner.position_tracker.open_position(
                updated.symbol,
                updated.deployment_id,
                trade_id=updated.trade_id,
                option_symbol=updated.option_symbol,
                quantity=updated.quantity,
                entry_price=updated.entry_price,
                underlying_entry_price=updated.underlying_entry_price,
                entry_timestamp=updated.entry_timestamp,
                source=updated.source,
                order_id=updated.order_id,
                stop_order_id=updated.stop_order_id,
                stop_price=updated.stop_price,
                target_order_id=updated.target_order_id,
                target_price=updated.target_price,
                exit_order_id=updated.exit_order_id,
                exit_limit_price=updated.exit_limit_price,
                exit_submitted_at=updated.exit_submitted_at,
                exit_mode=updated.exit_mode,
                exit_reprice_count=updated.exit_reprice_count,
            )
            if updated.trade_id is not None and updated.option_symbol is not None:
                await self._upsert_trade_record(
                    TradeRecord(
                        trade_id=updated.trade_id,
                        deployment_id=updated.deployment_id,
                        symbol=updated.symbol,
                        option_symbol=updated.option_symbol,
                        quantity=updated.quantity,
                        entry_price=updated.entry_price,
                        underlying_entry_price=updated.underlying_entry_price,
                        entry_timestamp=updated.entry_timestamp,
                        status=_tracked_trade_status(updated),
                        entry_order_id=updated.order_id,
                        stop_order_id=updated.stop_order_id,
                        stop_price=updated.stop_price,
                        target_order_id=updated.target_order_id,
                        target_price=updated.target_price,
                        exit_order_id=updated.exit_order_id,
                        exit_limit_price=updated.exit_limit_price,
                        exit_submitted_at=updated.exit_submitted_at,
                        exit_mode=updated.exit_mode,
                    )
                )
        return updated

    def _supports_concurrent_exit_orders(self) -> bool:
        return bool(getattr(self.planner.order_manager, "supports_concurrent_exit_orders", False))

    def _allows_exit_submission_before_cancel_confirmation(self) -> bool:
        return bool(getattr(self.planner.order_manager, "allows_exit_submission_before_cancel_confirmation", False))

    async def handle_exit(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> ExitPlan | None:
        async with self._symbol_locks[position.symbol]:
            return await self._handle_exit_locked(deployment, position, decision, dry_run=dry_run)

    async def _handle_exit_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        decision: ExitDecision,
        *,
        dry_run: bool,
    ) -> ExitPlan | None:
        await self.event_repository.append(
            "exit_decision",
            {
                "deployment_id": decision.deployment_id,
                "symbol": decision.symbol,
                "timestamp": decision.timestamp.isoformat(),
                "exit": decision.exit,
                "action": decision.action,
                "reason": decision.reason,
                "features": decision.features,
                "option_symbol": position.option_symbol,
                "quantity": position.quantity,
            },
        )
        if self.event_bus is not None:
            await self.event_bus.publish(ExitEvaluatedEvent(decision=decision))
        if not decision.exit or decision.action == "hold" or position.option_symbol is None or position.quantity <= 0:
            return None
        if decision.action != "square_off":
            return ExitPlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=deployment.deployment_id,
                symbol=position.symbol,
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                action=decision.action,
                reasons=decision.reason,
                dry_run=dry_run,
                canceled_stop_order_id=None,
                canceled_target_order_id=None,
                error=f"unsupported_exit_action:{decision.action}",
            )

        if position.exit_mode is not None or position.exit_order_id is not None:
            await self.event_repository.append(
                "exit_pending_status",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "exit_order_id": position.exit_order_id,
                    "status": "already_pending",
                    "exit_mode": position.exit_mode.value if position.exit_mode is not None else None,
                },
            )
            return None

        updated_position = position
        canceled_stop_order_id = None
        canceled_target_order_id = None
        cancel_error = None
        if decision.cancel_protection_orders:
            (
                updated_position,
                canceled_stop_order_id,
                canceled_target_order_id,
                cancel_error,
            ) = await self._cancel_exit_protection(
                deployment,
                updated_position,
                dry_run=dry_run,
                reason="strategy_exit",
            )

        if dry_run:
            fill_details = await self._paper_exit_fill_details(updated_position, order_id="DRY_RUN_EXIT")
            self.planner.position_tracker.close_position(
                updated_position.symbol,
                updated_position.deployment_id,
                option_symbol=updated_position.option_symbol,
            )
            if updated_position.trade_id is not None:
                await self.trade_state_repository.mark_closed(updated_position.trade_id, **fill_details)
            transition = self.lifecycle_store.mark_closed(updated_position.symbol, updated_position.deployment_id)
            await self._emit_lifecycle_transition(transition, reason="exit_closed")
            if updated_position.source == "shadow":
                await self._emit_shadow_exit_assumed(deployment, updated_position, fill_details, reason=decision.reason)
            plan = ExitPlan(
                trade_id=updated_position.trade_id or updated_position.order_id or "UNKNOWN_TRADE",
                deployment_id=deployment.deployment_id,
                symbol=updated_position.symbol,
                option_symbol=updated_position.option_symbol,
                quantity=updated_position.quantity,
                action=decision.action,
                reasons=decision.reason,
                dry_run=True,
                order_id="DRY_RUN_EXIT",
                canceled_stop_order_id=canceled_stop_order_id,
                canceled_target_order_id=canceled_target_order_id,
                error=cancel_error,
            )
            await self.event_repository.append("exit_plan", asdict(plan))
            await self._record_manual_status(
                deployment,
                stage="exit_closed",
                writer_call=self.manual_status_writer.mark_closed(
                    deployment,
                    trade_id=plan.trade_id,
                    note="dry_run_exit_closed",
                )
                if self.manual_status_writer is not None
                else None,
            )
            return plan

        updated_position, plan = await self._submit_exit_request(
            deployment,
            updated_position,
            exit_mode=ExitMode.STRATEGY,
            reason="exit_submitted",
            event_type="exit_submission",
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            inherited_error=cancel_error,
        )
        await self.event_repository.append("exit_plan", asdict(plan))
        await self._record_manual_status(
            deployment,
            stage="exit_submitted",
            writer_call=self.manual_status_writer.mark_exit_submitted(
                deployment,
                plan=plan,
            )
            if self.manual_status_writer is not None
            else None,
        )
        return plan

    async def _submit_exit_request(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        exit_mode: ExitMode,
        reason: str,
        event_type: str,
        canceled_stop_order_id: str | None = None,
        canceled_target_order_id: str | None = None,
        inherited_error: str | None = None,
        force_market: bool = False,
        submitted_at: datetime | None = None,
        increment_reprice: bool = False,
    ) -> tuple[TrackedPosition, ExitPlan]:
        if position.option_symbol is None:
            raise ValueError("Cannot submit exit without option_symbol")
        submitted_at = submitted_at or datetime.now(UTC)
        order_id: str | None = None
        limit_price: float | None = None
        error = inherited_error
        order_type = "MARKET" if exit_mode == ExitMode.EMERGENCY or force_market else "LIMIT"

        if exit_mode != ExitMode.EMERGENCY and not force_market:
            try:
                quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
            except Exception as exc:
                quote = None
                error = inherited_error or str(exc)
            limit_price = quote.exit_reference_price if quote is not None else None
            if limit_price is None:
                if exit_mode == ExitMode.STRATEGY:
                    order_type = "WAIT"
                else:
                    order_type = "MARKET"
        if order_type == "MARKET":
            if force_market and exit_mode != ExitMode.EMERGENCY:
                await self.event_repository.append(
                    "exit_market_fallback",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "exit_mode": exit_mode.value,
                        "reason": reason,
                    },
                )
            result = await self.planner.order_manager.place_close_order(
                position.option_symbol,
                position.quantity,
                exit_mode=exit_mode,
            )
            order_id = result.order_id
            error = result.error or error
            limit_price = None
        elif order_type == "LIMIT" and limit_price is not None:
            result = await self.planner.order_manager.place_close_order(
                position.option_symbol,
                position.quantity,
                exit_mode=exit_mode,
                limit_price=limit_price,
            )
            order_id = result.order_id
            error = result.error or error
        if order_type != "WAIT" and order_id is None:
            await self.event_repository.append(
                "exit_submission_failure",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "exit_mode": exit_mode.value,
                    "order_type": order_type,
                    "error": error,
                },
            )

        updated = _replace_position(
            position,
            exit_order_id=order_id,
            exit_limit_price=limit_price,
            exit_submitted_at=submitted_at,
            exit_mode=exit_mode,
            exit_reprice_count=position.exit_reprice_count + (1 if increment_reprice else 0),
        )
        self.planner.position_tracker.open_position(
            updated.symbol,
            updated.deployment_id,
            trade_id=updated.trade_id,
            option_symbol=updated.option_symbol,
            quantity=updated.quantity,
            entry_price=updated.entry_price,
            underlying_entry_price=updated.underlying_entry_price,
            entry_timestamp=updated.entry_timestamp,
            source=updated.source,
            order_id=updated.order_id,
            stop_order_id=updated.stop_order_id,
            stop_price=updated.stop_price,
            target_order_id=updated.target_order_id,
            target_price=updated.target_price,
            exit_order_id=updated.exit_order_id,
            exit_limit_price=updated.exit_limit_price,
            exit_submitted_at=updated.exit_submitted_at,
            exit_mode=updated.exit_mode,
            exit_reprice_count=updated.exit_reprice_count,
        )
        transition = self.lifecycle_store.mark_exit_pending(
            updated.symbol,
            updated.deployment_id,
            option_symbol=updated.option_symbol,
            order_id=updated.exit_order_id or updated.order_id,
        )
        await self._emit_lifecycle_transition(transition, reason=reason)
        if updated.trade_id is not None and updated.option_symbol is not None:
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=updated.trade_id,
                    deployment_id=updated.deployment_id,
                    symbol=updated.symbol,
                    option_symbol=updated.option_symbol,
                    quantity=updated.quantity,
                    entry_price=updated.entry_price,
                    underlying_entry_price=updated.underlying_entry_price,
                    entry_timestamp=updated.entry_timestamp,
                    status="exit_pending",
                    entry_order_id=updated.order_id,
                    stop_order_id=updated.stop_order_id,
                    stop_price=updated.stop_price,
                    target_order_id=updated.target_order_id,
                    target_price=updated.target_price,
                    exit_order_id=updated.exit_order_id,
                    exit_limit_price=updated.exit_limit_price,
                    exit_submitted_at=updated.exit_submitted_at,
                    exit_mode=updated.exit_mode,
                )
            )
        await self.event_repository.append(
            event_type,
            {
                "deployment_id": deployment.deployment_id,
                "symbol": updated.symbol,
                "option_symbol": updated.option_symbol,
                "quantity": updated.quantity,
                "exit_mode": exit_mode.value,
                "order_id": updated.exit_order_id,
                "order_type": order_type,
                "limit_price": updated.exit_limit_price,
                "error": error,
                "exit_submitted_at": updated.exit_submitted_at.isoformat() if updated.exit_submitted_at is not None else None,
            },
        )
        plan = ExitPlan(
            trade_id=updated.trade_id or updated.order_id or "UNKNOWN_TRADE",
            deployment_id=updated.deployment_id,
            symbol=updated.symbol,
            option_symbol=updated.option_symbol,
            quantity=updated.quantity,
            action="square_off",
            reasons=[reason],
            dry_run=False,
            order_id=updated.exit_order_id,
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            error=error,
        )
        return updated, plan

    async def _cancel_exit_protection(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
        reason: str,
    ) -> tuple[TrackedPosition, str | None, str | None, str | None]:
        updated = position
        canceled_stop_order_id = None
        canceled_target_order_id = None
        first_error = None
        if position.stop_order_id:
            canceled = True
            cancel_error = None
            if not dry_run:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.stop_order_id)
            if canceled:
                canceled_stop_order_id = position.stop_order_id
                updated = _replace_position(updated, stop_order_id=None)
            elif self._allows_exit_submission_before_cancel_confirmation():
                await self.event_repository.append(
                    "ambiguous_cancel",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "order_id": position.stop_order_id,
                        "kind": "stop",
                        "reason": reason,
                        "error": cancel_error,
                    },
                )
            await self.event_repository.append(
                "protection_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "stop_order_id": position.stop_order_id,
                    "canceled": canceled,
                    "error": cancel_error,
                    "reason": reason,
                },
            )
            first_error = cancel_error
        if position.target_order_id:
            canceled = True
            cancel_error = None
            if not dry_run:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.target_order_id)
            if canceled:
                canceled_target_order_id = position.target_order_id
                updated = _replace_position(updated, target_order_id=None)
            elif self._allows_exit_submission_before_cancel_confirmation():
                await self.event_repository.append(
                    "ambiguous_cancel",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "order_id": position.target_order_id,
                        "kind": "target",
                        "reason": reason,
                        "error": cancel_error,
                    },
                )
            await self.event_repository.append(
                "target_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "target_order_id": position.target_order_id,
                    "canceled": canceled,
                    "error": cancel_error,
                    "reason": reason,
                },
            )
            if first_error is None:
                first_error = cancel_error
        return updated, canceled_stop_order_id, canceled_target_order_id, first_error

    async def manage_pending_exits(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        now: datetime | None = None,
    ) -> list[ExitPlan]:
        plans: list[ExitPlan] = []
        current_now = now or datetime.now(UTC)
        for position in list(self.planner.position_tracker.active_positions()):
            if position.exit_mode is None and position.exit_order_id is None and position.exit_submitted_at is None:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None:
                continue
            async with self._symbol_locks[position.symbol]:
                plan = await self._manage_pending_exit_locked(deployment, position, now=current_now)
            if plan is not None:
                plans.append(plan)
        return plans

    async def _manage_pending_exit_locked(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        now: datetime,
    ) -> ExitPlan | None:
        if position.option_symbol is None or position.quantity <= 0 or position.exit_mode is None:
            return None
        status = None
        payload = None
        error = None
        if position.exit_order_id:
            status, payload, error = await self.planner.order_manager.get_order_status(position.exit_order_id)
        await self.event_repository.append(
            "exit_pending_status",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "option_symbol": position.option_symbol,
                "exit_order_id": position.exit_order_id,
                "status": status,
                "error": error,
                "exit_mode": position.exit_mode.value,
            },
        )
        normalized = (status or "").upper()
        if normalized == "FILLED":
            self.planner.position_tracker.close_position(
                position.symbol,
                position.deployment_id,
                option_symbol=position.option_symbol,
            )
            if position.trade_id is not None:
                await self._mark_trade_closed_with_exit_truth(
                    position.trade_id,
                    exit_order_id=position.exit_order_id,
                    status=status,
                    payload=payload,
                )
            transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
            await self._emit_lifecycle_transition(transition, reason="exit_closed")
            plan = ExitPlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=deployment.deployment_id,
                symbol=position.symbol,
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                action="square_off",
                reasons=["exit_filled"],
                dry_run=False,
                order_id=position.exit_order_id,
            )
            await self.event_repository.append("exit_plan", asdict(plan))
            await self._record_manual_status(
                deployment,
                stage="exit_closed",
                writer_call=self.manual_status_writer.mark_closed(
                    deployment,
                    trade_id=position.trade_id,
                    note="exit_filled",
                    event_at=now,
                )
                if self.manual_status_writer is not None
                else None,
            )
            return plan
        if error and position.exit_order_id is not None:
            await self.event_repository.append(
                "ambiguous_cancel",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "order_id": position.exit_order_id,
                    "kind": "exit_status",
                    "reason": "status_unavailable",
                    "error": error,
                },
            )
            return None
        if normalized in {"REJECTED", "CANCELED", "EXPIRED"} or position.exit_order_id is None:
            _, plan = await self._submit_exit_request(
                deployment,
                position,
                exit_mode=position.exit_mode,
                reason="exit_resubmitted",
                event_type="exit_resubmitted",
                inherited_error=error,
                submitted_at=position.exit_submitted_at,
                force_market=position.exit_mode == ExitMode.EMERGENCY,
            )
            return plan
        if normalized in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            if position.exit_mode == ExitMode.STRATEGY:
                try:
                    quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
                except Exception:
                    quote = None
                next_price = quote.exit_reference_price if quote is not None else None
                if next_price is not None and _material_exit_price_change(position.exit_limit_price, next_price):
                    replaced_position, _, _, cancel_error = await self._cancel_exit_protection(
                        deployment,
                        position,
                        dry_run=False,
                        reason="exit_reprice",
                    )
                    if position.exit_order_id:
                        canceled, replace_cancel_error = await self.planner.order_manager.cancel_order(position.exit_order_id)
                        if not canceled and self._allows_exit_submission_before_cancel_confirmation():
                            await self.event_repository.append(
                                "ambiguous_cancel",
                                {
                                    "deployment_id": deployment.deployment_id,
                                    "symbol": position.symbol,
                                    "option_symbol": position.option_symbol,
                                    "order_id": position.exit_order_id,
                                    "kind": "exit",
                                    "reason": "exit_reprice",
                                    "error": replace_cancel_error,
                                },
                            )
                        if cancel_error is None:
                            cancel_error = replace_cancel_error
                    _, plan = await self._submit_exit_request(
                        deployment,
                        replaced_position,
                        exit_mode=ExitMode.STRATEGY,
                        reason="exit_reprice",
                        event_type="exit_reprice",
                        inherited_error=cancel_error,
                        submitted_at=position.exit_submitted_at,
                    )
                    return plan
            if position.exit_mode == ExitMode.HARD_FLAT:
                if position.exit_reprice_count == 0:
                    if position.exit_order_id:
                        canceled, cancel_error = await self.planner.order_manager.cancel_order(position.exit_order_id)
                        if not canceled and self._allows_exit_submission_before_cancel_confirmation():
                            await self.event_repository.append(
                                "ambiguous_cancel",
                                {
                                    "deployment_id": deployment.deployment_id,
                                    "symbol": position.symbol,
                                    "option_symbol": position.option_symbol,
                                    "order_id": position.exit_order_id,
                                    "kind": "exit",
                                    "reason": "hard_flat_reprice",
                                    "error": cancel_error,
                                },
                            )
                    _, plan = await self._submit_exit_request(
                        deployment,
                        position,
                        exit_mode=ExitMode.HARD_FLAT,
                        reason="hard_flat_reprice",
                        event_type="exit_reprice",
                        inherited_error=cancel_error if 'cancel_error' in locals() else None,
                        submitted_at=position.exit_submitted_at,
                        increment_reprice=True,
                    )
                    return plan
                if _hard_flat_market_fallback_due(position.exit_submitted_at, now, deployment):
                    _, plan = await self._submit_exit_request(
                        deployment,
                        position,
                        exit_mode=ExitMode.HARD_FLAT,
                        reason="hard_flat_market_fallback",
                        event_type="exit_resubmitted",
                        submitted_at=position.exit_submitted_at,
                        force_market=True,
                    )
                    return plan
        return None

    async def close_due_positions(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        now: datetime,
        dry_run: bool,
        symbol: str | None = None,
    ) -> list[TradePlan]:
        closed: list[TradePlan] = []
        now_et = as_et_time(now)
        for position in self.planner.position_tracker.active_positions():
            if symbol is not None and position.symbol != symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None or position.option_symbol is None or position.quantity <= 0:
                continue
            if position.exit_mode is not None or position.exit_order_id is not None:
                continue
            position_dry_run = dry_run or position.source == "shadow"
            hard_flat_time = parse_time_text(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")
            if now_et < hard_flat_time:
                continue

            order_id = "DRY_RUN_CLOSE"
            error = None
            if position_dry_run:
                fill_details = await self._paper_exit_fill_details(position, order_id=order_id)
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, **fill_details)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="hard_flat_closed")
                if position.source == "shadow":
                    await self._emit_shadow_exit_assumed(deployment, position, fill_details, reason=["hard_flat_time_reached"])
                await self._record_manual_status(
                    deployment,
                    stage="hard_flat_closed",
                    writer_call=self.manual_status_writer.mark_closed(
                        deployment,
                        trade_id=position.trade_id,
                        note="hard_flat_closed",
                        event_at=now,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            else:
                updated_position, canceled_stop_order_id, canceled_target_order_id, cancel_error = await self._cancel_exit_protection(
                    deployment,
                    position,
                    dry_run=False,
                    reason="hard_flat",
                )
                updated_position, exit_plan = await self._submit_exit_request(
                    deployment,
                    updated_position,
                    exit_mode=ExitMode.HARD_FLAT,
                    reason="hard_flat_submitted",
                    event_type="exit_submission",
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    inherited_error=cancel_error,
                )
                order_id = exit_plan.order_id
                error = exit_plan.error
                await self._record_manual_status(
                    deployment,
                    stage="hard_flat_submitted",
                    writer_call=self.manual_status_writer.mark_exit_submitted(
                        deployment,
                        plan=exit_plan,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            trade_plan = TradePlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=position.deployment_id,
                symbol=position.symbol,
                direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                estimated_entry_price=0.0,
                risk_reasons=["hard_flat_time_reached"],
                dry_run=position_dry_run,
                order_id=order_id,
            )
            await self.event_repository.append(
                "hard_flat_submission",
                {
                    "deployment_id": position.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "order_id": order_id,
                    "error": error,
                },
            )
            closed.append(trade_plan)
        return closed

    async def halt_and_flatten_positions(
        self,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        dry_run: bool,
        symbol: str | None = None,
    ) -> list[TradePlan]:
        closed: list[TradePlan] = []
        for position in self.planner.position_tracker.active_positions():
            if symbol is not None and position.symbol != symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None or position.option_symbol is None or position.quantity <= 0:
                continue
            if position.exit_mode is not None or position.exit_order_id is not None:
                continue
            position_dry_run = dry_run or position.source == "shadow"

            order_id = "DRY_RUN_EMERGENCY_FLAT"
            error = None
            canceled_stop_order_id = None
            canceled_target_order_id = None
            if position.source == "live_pending" and position.order_id:
                canceled, cancel_error = await self.planner.order_manager.cancel_order(position.order_id)
                await self.event_repository.append(
                    "entry_cancel_attempt",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "entry_order_id": position.order_id,
                        "canceled": canceled,
                        "error": cancel_error,
                        "reason": "halt_and_flatten",
                    },
                )
                if not canceled:
                    await self.event_repository.append(
                        "halt_and_flatten_failure",
                        {
                            "deployment_id": position.deployment_id,
                            "symbol": position.symbol,
                            "option_symbol": position.option_symbol,
                            "quantity": position.quantity,
                            "error": cancel_error,
                            "reason": "pending_entry_cancel_failed",
                        },
                    )
                    continue
                order_id = position.order_id
                error = cancel_error
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, exit_order_id=order_id)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="halt_and_flatten_pending_entry_canceled")
                trade_plan = TradePlan(
                    trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                    deployment_id=position.deployment_id,
                    symbol=position.symbol,
                    direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    estimated_entry_price=0.0,
                    risk_reasons=["halt_and_flatten_triggered"],
                    dry_run=dry_run,
                    order_id=order_id,
                )
                await self.event_repository.append(
                    "halt_and_flatten_submission",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "quantity": position.quantity,
                        "order_id": order_id,
                        "error": error,
                        "mode": "pending_entry_cancel",
                    },
                )
                closed.append(trade_plan)
                continue
            if position_dry_run:
                fill_details = await self._paper_exit_fill_details(position, order_id=order_id)
                self.planner.position_tracker.close_position(
                    position.symbol,
                    position.deployment_id,
                    option_symbol=position.option_symbol,
                )
                if position.trade_id is not None:
                    await self.trade_state_repository.mark_closed(position.trade_id, **fill_details)
                transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
                await self._emit_lifecycle_transition(transition, reason="halt_and_flatten_closed")
                if position.source == "shadow":
                    await self._emit_shadow_exit_assumed(deployment, position, fill_details, reason=["halt_and_flatten_triggered"])
                await self._record_manual_status(
                    deployment,
                    stage="halt_and_flatten_closed",
                    writer_call=self.manual_status_writer.mark_closed(
                        deployment,
                        trade_id=position.trade_id,
                        note="halt_and_flatten_closed",
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            else:
                updated_position, canceled_stop_order_id, canceled_target_order_id, cancel_error = await self._cancel_exit_protection(
                    deployment,
                    position,
                    dry_run=False,
                    reason="halt_and_flatten",
                )
                updated_position, exit_plan = await self._submit_exit_request(
                    deployment,
                    updated_position,
                    exit_mode=ExitMode.EMERGENCY,
                    reason="halt_and_flatten_submitted",
                    event_type="exit_submission",
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    inherited_error=cancel_error,
                    force_market=True,
                )
                order_id = exit_plan.order_id
                error = exit_plan.error
                await self._record_manual_status(
                    deployment,
                    stage="halt_and_flatten_submitted",
                    writer_call=self.manual_status_writer.mark_exit_submitted(
                        deployment,
                        plan=exit_plan,
                    )
                    if self.manual_status_writer is not None
                    else None,
                )
            trade_plan = TradePlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=position.deployment_id,
                symbol=position.symbol,
                direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                estimated_entry_price=0.0,
                risk_reasons=["halt_and_flatten_triggered"],
                dry_run=position_dry_run,
                order_id=order_id,
            )
            await self.event_repository.append(
                "halt_and_flatten_submission",
                {
                    "deployment_id": position.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "quantity": position.quantity,
                    "order_id": order_id,
                    "error": error,
                    "canceled_stop_order_id": canceled_stop_order_id,
                    "canceled_target_order_id": canceled_target_order_id,
                },
            )
            closed.append(trade_plan)
        return closed

    async def sync_lifecycle(self) -> None:
        transitions = self.lifecycle_store.sync_from_positions(self.planner.position_tracker.active_positions())
        for transition in transitions:
            await self._emit_lifecycle_transition(transition, reason="broker_reconciliation_sync")
        recent_trades = await self.trade_state_repository.get_recent_trades(limit=200)
        recent_trade_ids = {trade.trade_id for trade in recent_trades}
        open_trades = await self.trade_state_repository.get_open_trades()
        open_trades_by_id = {trade.trade_id: trade for trade in open_trades}
        active_trade_ids = {
            position.trade_id
            for position in self.planner.position_tracker.active_positions()
            if position.trade_id is not None
        }
        for trade in open_trades:
            if trade.status == "pending_entry":
                continue
            if trade.status == "pending_entry_reconcile":
                if trade.trade_id in active_trade_ids:
                    continue
                await self._reconcile_pending_entry_release(trade)
                continue
            if trade.trade_id not in active_trade_ids:
                await self._mark_disappeared_trade_closed(trade)
        await self._enrich_recent_closed_exit_truth(recent_trades)
        for position in self.planner.position_tracker.active_positions():
            if position.trade_id is None or position.option_symbol is None:
                continue
            if position.source == "broker_recovered" and position.trade_id not in recent_trade_ids:
                await self.event_repository.append(
                    "orphan_position_recovered",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "trade_id": position.trade_id,
                        "option_symbol": position.option_symbol,
                        "quantity": position.quantity,
                        "entry_price": position.entry_price,
                        "entry_timestamp": position.entry_timestamp.isoformat() if position.entry_timestamp else None,
                    },
                )
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=position.trade_id,
                    deployment_id=position.deployment_id,
                    symbol=position.symbol,
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    underlying_entry_price=position.underlying_entry_price,
                    entry_timestamp=position.entry_timestamp,
                    status=_tracked_trade_status(position),
                    entry_order_id=position.order_id,
                    stop_order_id=position.stop_order_id,
                    stop_price=position.stop_price,
                    target_order_id=position.target_order_id,
                    target_price=position.target_price,
                    exit_order_id=position.exit_order_id,
                    exit_limit_price=position.exit_limit_price,
                    exit_submitted_at=position.exit_submitted_at,
                    exit_mode=position.exit_mode,
                )
            )
            previous = open_trades_by_id.get(position.trade_id)
            if previous is not None and previous.status == "pending_entry_reconcile":
                await self.event_repository.append(
                    "entry_reconcile_recovered",
                    {
                        "deployment_id": position.deployment_id,
                        "symbol": position.symbol,
                        "trade_id": position.trade_id,
                        "option_symbol": position.option_symbol,
                        "entry_order_id": position.order_id,
                    },
                )
        await self._sync_cash_guard()

    async def _mark_disappeared_trade_closed(self, trade: TradeRecord) -> None:
        exit_order_id, status, payload = await self._find_terminal_exit_order_payload(trade)
        await self._mark_trade_closed_with_exit_truth(
            trade.trade_id,
            exit_order_id=exit_order_id or trade.exit_order_id,
            status=status,
            payload=payload,
        )
        if exit_order_id is not None and payload is not None:
            await self.event_repository.append(
                "exit_fill_enriched",
                {
                    "deployment_id": trade.deployment_id,
                    "symbol": trade.symbol,
                    "trade_id": trade.trade_id,
                    "option_symbol": trade.option_symbol,
                    "exit_order_id": exit_order_id,
                    "status": status,
                    "source": "disappeared_position_reconcile",
                    "payload": payload,
                },
            )

    async def _enrich_recent_closed_exit_truth(self, trades: list[TradeRecord]) -> None:
        for trade in trades:
            if trade.status != "closed" or trade.exit_price is not None:
                continue
            if not any((trade.exit_order_id, trade.stop_order_id, trade.target_order_id)):
                continue
            exit_order_id, status, payload = await self._find_terminal_exit_order_payload(trade)
            if exit_order_id is None or payload is None:
                continue
            await self._mark_trade_closed_with_exit_truth(
                trade.trade_id,
                exit_order_id=exit_order_id,
                status=status,
                payload=payload,
            )
            await self.event_repository.append(
                "exit_fill_enriched",
                {
                    "deployment_id": trade.deployment_id,
                    "symbol": trade.symbol,
                    "trade_id": trade.trade_id,
                    "option_symbol": trade.option_symbol,
                    "exit_order_id": exit_order_id,
                    "status": status,
                    "source": "recent_closed_retry",
                    "payload": payload,
                },
            )

    async def _find_terminal_exit_order_payload(self, trade: TradeRecord) -> tuple[str | None, str | None, dict | None]:
        seen: set[str] = set()
        order_ids = [
            trade.exit_order_id,
            trade.stop_order_id,
            trade.target_order_id,
        ]
        for order_id in order_ids:
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            status, payload, error = await self.planner.order_manager.get_order_status(order_id)
            if error or payload is None:
                continue
            if _is_filled_exit_order(payload, status=status, option_symbol=trade.option_symbol):
                return order_id, status, payload
        return None, None, None

    async def _mark_trade_closed_with_exit_truth(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None,
        status: str | None,
        payload: dict | None,
    ) -> None:
        details = _exit_fill_details(payload, status=status)
        await self.trade_state_repository.mark_closed(
            trade_id,
            exit_order_id=exit_order_id,
            **details,
        )

    async def _reconcile_pending_entry_release(self, trade: TradeRecord) -> None:
        if trade.entry_order_id is None:
            return
        status, payload, error = await self.planner.order_manager.get_order_status(trade.entry_order_id)
        normalized = (status or error or "").upper()
        if normalized not in {"REJECTED", "CANCELED", "EXPIRED"}:
            return
        await self._release_cash_guard_reservation(trade.trade_id)
        await self.trade_state_repository.mark_closed(trade.trade_id, exit_order_id=trade.exit_order_id)
        transition = self.lifecycle_store.mark_closed(trade.symbol, trade.deployment_id)
        await self._emit_lifecycle_transition(transition, reason="entry_reconcile_released")
        await self.event_repository.append(
            "entry_reconcile_released",
            {
                "deployment_id": trade.deployment_id,
                "symbol": trade.symbol,
                "trade_id": trade.trade_id,
                "entry_order_id": trade.entry_order_id,
                "status": normalized,
                "payload": payload or {},
            },
        )

    async def _restore_missing_protection(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition:
        stop_loss_pct, policy = _resolved_recovery_stop_loss_pct(deployment)
        if stop_loss_pct is None or stop_loss_pct <= 0:
            return position
        existing_protection = None if dry_run else await self._find_active_close_order(position.option_symbol)
        if existing_protection is not None:
            updated = _replace_position(
                position,
                stop_order_id=existing_protection["order_id"] if existing_protection["type"] == "STOP" else None,
                stop_price=existing_protection["price"] if existing_protection["type"] == "STOP" else None,
                target_order_id=existing_protection["order_id"] if existing_protection["type"] == "LIMIT" else None,
                target_price=existing_protection["price"] if existing_protection["type"] == "LIMIT" else None,
            )
            await self.event_repository.append(
                "protection_restore_skipped",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "trade_id": position.trade_id,
                    "option_symbol": position.option_symbol,
                    "reason": "active_close_order_exists",
                    "order_id": existing_protection["order_id"],
                    "order_type": existing_protection["type"],
                    "price": existing_protection["price"],
                },
            )
            transition = (
                self.lifecycle_store.mark_target_active(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=updated.target_order_id,
                )
                if updated.target_order_id
                else self.lifecycle_store.mark_open(
                    updated.symbol,
                    updated.deployment_id,
                    option_symbol=updated.option_symbol,
                    order_id=updated.stop_order_id,
                    protected=True,
                )
            )
            await self._emit_lifecycle_transition(transition, reason="protection_reconciled")
            return updated
        await self.event_repository.append(
            "protection_restore_attempt",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "policy": policy,
                "dry_run": dry_run,
            },
        )
        stop_result, stop_price, target_order_id, target_price = await self._arm_position_protection(
            deployment,
            option_symbol=position.option_symbol,
            quantity=position.quantity,
            entry_price=position.entry_price,
            dry_run=dry_run,
            event_payload={
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "policy": policy,
                "source": position.source,
                "dry_run": dry_run,
            },
        )
        if stop_result.order_id is None:
            await self.event_repository.append(
                "runtime_issue",
                {
                    "category": "protection_restore",
                    "symbol": position.symbol,
                    "deployment_id": deployment.deployment_id,
                    "trade_id": position.trade_id,
                    "error": stop_result.error or "missing_stop_order_id",
                    "stage": "protection_restore",
                },
            )
            return position
        updated = _replace_position(
            position,
            stop_order_id=stop_result.order_id,
            stop_price=stop_price,
            target_order_id=target_order_id,
            target_price=target_price,
        )
        transition = (
            self.lifecycle_store.mark_target_active(
                updated.symbol,
                updated.deployment_id,
                option_symbol=updated.option_symbol,
                order_id=target_order_id,
            )
            if target_order_id
            else self.lifecycle_store.mark_open(
                updated.symbol,
                updated.deployment_id,
                option_symbol=updated.option_symbol,
                order_id=stop_result.order_id,
                protected=True,
            )
        )
        await self._emit_lifecycle_transition(transition, reason="protection_restored")
        return updated

    async def _find_active_close_order(self, option_symbol: str | None) -> dict[str, object] | None:
        if option_symbol is None:
            return None
        try:
            portfolio = await self.planner.order_manager.get_portfolio()
        except Exception:
            return None
        normalized_symbol = normalize_option_symbol(option_symbol)
        for order in portfolio.get("orders", []) or []:
            instrument = order.get("instrument", {}) or {}
            if instrument.get("type") != "OPTION":
                continue
            if normalize_option_symbol(str(instrument.get("symbol", ""))) != normalized_symbol:
                continue
            if str(order.get("side", "")).upper() != "SELL":
                continue
            if str(order.get("openCloseIndicator", "")).upper() != "CLOSE":
                continue
            if str(order.get("status", "")).upper() not in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
                continue
            order_type = str(order.get("type", "")).upper()
            if order_type == "STOP":
                price = _maybe_float(order.get("stopPrice"))
            elif order_type == "LIMIT":
                price = _maybe_float(order.get("limitPrice"))
            else:
                continue
            order_id = order.get("orderId")
            if not order_id:
                continue
            return {"order_id": str(order_id), "type": order_type, "price": price}
        return None

    async def _arm_position_protection(
        self,
        deployment: DeploymentManifest,
        *,
        option_symbol: str,
        quantity: int,
        entry_price: float,
        dry_run: bool,
        event_payload: dict[str, object],
    ):
        stop_loss_pct, _ = _resolved_recovery_stop_loss_pct(deployment)
        requested_stop_price = entry_price * (1.0 - (stop_loss_pct or 0.0))
        stop_price = requested_stop_price
        quote_bid = None
        stop_sanitized = False
        stop_sanitized_reason = None
        if event_payload.get("source") == "broker_sync":
            stop_price, quote_bid, stop_sanitized_reason = await self._sanitize_recovered_stop_price(
                option_symbol,
                requested_stop_price,
            )
            stop_sanitized = stop_sanitized_reason is not None
        if stop_price is None:
            stop_result = OrderResult(order_id=None, error="recovered_stop_sanitization_failed")
        else:
            stop_result = (
                _DryRunOrderResult("DRY_RUN_STOP")
                if dry_run
                else await self.planner.order_manager.place_stop_loss_order(option_symbol, stop_price, quantity)
            )
        target_order_id = None
        target_price = None
        if _profit_target_configured(deployment):
            target_price = _deployment_target_price(deployment, entry_price)
            if self._supports_concurrent_exit_orders():
                target_result = (
                    _DryRunOrderResult("DRY_RUN_TARGET")
                    if dry_run
                    else await self.planner.order_manager.place_target_order(option_symbol, target_price, quantity)
                )
                target_order_id = target_result.order_id
                await self.event_repository.append(
                    "profit_target_submission",
                    {
                        **event_payload,
                        "target_order_id": target_result.order_id,
                        "target_error": target_result.error,
                        "target_price": target_price,
                    },
                )
            else:
                await self.event_repository.append(
                    "profit_target_armed",
                    {
                        **event_payload,
                        "target_order_id": None,
                        "target_price": target_price,
                        "mode": "virtual",
                        "reason": "single_resting_exit_order_broker",
                    },
                )
        await self.event_repository.append(
            "protective_stop_submission",
            {
                **event_payload,
                "stop_order_id": stop_result.order_id,
                "stop_error": stop_result.error,
                "stop_price": stop_price,
                "requested_stop_price": round_price(requested_stop_price),
                "quote_bid": quote_bid,
                "stop_sanitized": stop_sanitized,
                "stop_sanitized_reason": stop_sanitized_reason,
            },
        )
        return stop_result, stop_price, target_order_id, target_price

    async def _sanitize_recovered_stop_price(
        self,
        option_symbol: str,
        requested_stop_price: float,
    ) -> tuple[float | None, float | None, str | None]:
        quote = await self.planner.order_manager.get_option_quote(option_symbol)
        if quote.bid is None:
            return round_price(requested_stop_price), None, None
        requested = round_price(requested_stop_price)
        max_valid_stop = _max_valid_sell_stop_price(quote.bid)
        if max_valid_stop is None:
            return None, quote.bid, "no_valid_bid_buffer"
        if requested < quote.bid:
            return requested, quote.bid, None
        sanitized = min(requested, max_valid_stop)
        return sanitized, quote.bid, "below_bid_buffer"

    async def _emit_lifecycle_transition(
        self,
        transition: LifecycleTransition | None,
        *,
        reason: str,
    ) -> None:
        if transition is None:
            return
        payload = {
            "symbol": transition.symbol,
            "deployment_id": transition.deployment_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_state": transition.previous_state.value if transition.previous_state else None,
            "new_state": transition.new_state.value,
            "option_symbol": transition.option_symbol,
            "order_id": transition.order_id,
            "reason": reason,
        }
        await self.event_repository.append("lifecycle_transition", payload)
        if self.event_bus is not None:
            await self.event_bus.publish(
                TradeLifecycleTransitionEvent(
                    symbol=transition.symbol,
                    deployment_id=transition.deployment_id,
                    timestamp=datetime.now(UTC),
                    previous_state=transition.previous_state.value if transition.previous_state else None,
                    new_state=transition.new_state.value,
                    option_symbol=transition.option_symbol,
                    order_id=transition.order_id,
                    reason=reason,
                )
            )

    async def _record_manual_status(
        self,
        deployment: DeploymentManifest,
        *,
        stage: str,
        writer_call,
    ) -> None:
        if writer_call is None:
            return
        error = await writer_call
        if error is None:
            return
        await self.event_repository.append(
            "sheet_status_writeback_failure",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": deployment.symbol,
                "stage": stage,
                "error": error,
            },
        )

    async def _upsert_trade_record(self, record: TradeRecord) -> None:
        await self.trade_state_repository.upsert_trade(record)

    async def _finalize_cash_guard_reservation(self, trade_id: str) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.finalize_entry(trade_id)

    async def _release_cash_guard_reservation(self, trade_id: str) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.release_entry(trade_id)

    async def _paper_exit_fill_details(self, position: TrackedPosition, *, order_id: str) -> dict[str, Any]:
        exit_price = None
        payload = None
        status = "FILLED"
        if position.option_symbol:
            quote = await self.planner.order_manager.get_option_quote(position.option_symbol)
            exit_price = quote.exit_reference_price
            payload = {
                "source": "paper_shadow" if position.source == "shadow" else "dry_run",
                "symbol": position.option_symbol,
                "bid": quote.bid,
                "ask": quote.ask,
                "last": quote.last,
                "spread_pct": quote.spread_pct,
                "averagePrice": exit_price,
                "filledQuantity": position.quantity,
                "closedAt": datetime.now(UTC).isoformat(),
                "status": status,
                "type": "PAPER",
            }
        return {
            "exit_order_id": order_id,
            "exit_price": exit_price,
            "exit_filled_quantity": position.quantity if exit_price is not None else None,
            "exit_filled_at": datetime.now(UTC) if exit_price is not None else None,
            "exit_order_status": status if exit_price is not None else None,
            "exit_order_type": "PAPER" if exit_price is not None else None,
            "exit_broker_payload": payload,
        }

    async def _emit_shadow_exit_assumed(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        fill_details: dict[str, Any],
        *,
        reason: list[str],
    ) -> None:
        exit_price = fill_details.get("exit_price")
        await self.event_repository.append(
            "shadow_exit_assumed",
            {
                "deployment_id": deployment.deployment_id,
                "symbol": position.symbol,
                "trade_id": position.trade_id,
                "option_symbol": position.option_symbol,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "realized_pnl_usd": _premium_pnl(position.entry_price, exit_price, position.quantity),
                "realized_stop_r": _realized_stop_r(
                    position.entry_price,
                    exit_price,
                    deployment.exit.stop_loss_pct or deployment.risk.stop_loss_pct,
                ),
                "exit_order_id": fill_details.get("exit_order_id"),
                "reason": list(reason),
            },
        )

    async def _sync_cash_guard(self) -> None:
        cash_guard = getattr(self.planner, "cash_guard", None)
        if cash_guard is None:
            return
        await cash_guard.sync_positions(
            self.planner.position_tracker.active_positions(),
            await self.trade_state_repository.get_open_trades(),
        )


def _profit_target_configured(deployment: DeploymentManifest) -> bool:
    return bool(
        deployment.exit.use_profit_target
        and (
            deployment.exit.option_profit_target_pct is not None
            or deployment.exit.profit_target_multiple is not None
        )
    )


def _deployment_target_price(deployment: DeploymentManifest, entry_price: float) -> float:
    return _target_price(
        entry_price,
        deployment.exit.stop_loss_pct,
        deployment.exit.profit_target_multiple,
        option_profit_target_pct=deployment.exit.option_profit_target_pct,
    )


def _target_price(
    entry_price: float,
    stop_loss_pct: float,
    r_multiple: float | None,
    *,
    option_profit_target_pct: float | None = None,
) -> float:
    if option_profit_target_pct is not None:
        return entry_price * (1.0 + option_profit_target_pct)
    return entry_price * (1.0 + stop_loss_pct * (r_multiple or 0.0))


def _max_valid_sell_stop_price(bid: float) -> float | None:
    if bid <= 0.01:
        return None
    candidate = math.floor(((bid - 0.01) + 1e-9) * 100.0) / 100.0
    if candidate <= 0:
        return None
    return round_price(candidate)


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _maybe_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_filled_exit_order(payload: dict[str, Any], *, status: str | None, option_symbol: str | None) -> bool:
    normalized_status = str(status or payload.get("status") or "").upper()
    if normalized_status != "FILLED":
        return False
    side = str(payload.get("side") or payload.get("orderSide") or "").upper()
    open_close = str(payload.get("openCloseIndicator") or "").upper()
    instrument_symbol = normalize_option_symbol(str((payload.get("instrument") or {}).get("symbol", "")))
    if option_symbol is not None and instrument_symbol and instrument_symbol != normalize_option_symbol(option_symbol):
        return False
    return side == "SELL" and open_close == "CLOSE"


def _exit_fill_details(payload: dict | None, *, status: str | None) -> dict[str, Any]:
    if payload is None:
        return {
            "exit_price": None,
            "exit_filled_quantity": None,
            "exit_filled_at": None,
            "exit_order_status": status,
            "exit_order_type": None,
            "exit_broker_payload": None,
        }
    return {
        "exit_price": _maybe_float(payload.get("averagePrice")),
        "exit_filled_quantity": _maybe_int(payload.get("filledQuantity")),
        "exit_filled_at": _maybe_datetime(payload.get("closedAt")),
        "exit_order_status": status or payload.get("status"),
        "exit_order_type": payload.get("type"),
        "exit_broker_payload": payload,
    }


def _premium_pnl(entry_price: float | None, exit_price: float | None, quantity: int | None) -> float | None:
    if entry_price is None or exit_price is None or not quantity:
        return None
    return round((exit_price - entry_price) * int(quantity) * 100.0, 2)


def _realized_stop_r(entry_price: float | None, exit_price: float | None, stop_loss_pct: float | None) -> float | None:
    if entry_price is None or exit_price is None or stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    risk_per_contract = entry_price * stop_loss_pct
    if risk_per_contract <= 0:
        return None
    return round((exit_price - entry_price) / risk_per_contract, 4)


def _underlying_entry_price(decision: SignalDecision) -> float | None:
    value = decision.features.get("close")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_plan_approved(plan: TradePlan) -> bool:
    return plan.risk_reasons == ["approved"]


def _restore_threshold_price(entry_price: float, target_price: float, progress_pct: float) -> float:
    progress = max(0.0, min(1.0, progress_pct))
    return entry_price + ((target_price - entry_price) * progress)


def _tracked_trade_status(position: TrackedPosition) -> str:
    if position.exit_mode is not None or position.exit_order_id is not None or position.exit_submitted_at is not None:
        return "exit_pending"
    if position.target_order_id:
        return "target_active"
    return "open_protected" if position.stop_order_id else "open_unprotected"


def _resolved_recovery_stop_loss_pct(deployment: DeploymentManifest) -> tuple[float | None, str]:
    if deployment.exit.stop_loss_pct is not None and deployment.exit.stop_loss_pct > 0:
        return deployment.exit.stop_loss_pct, "deployment_native"
    if deployment.risk.stop_loss_pct is not None and deployment.risk.stop_loss_pct > 0:
        return deployment.risk.stop_loss_pct, "global_fallback"
    return None, "unavailable"


class _DryRunOrderResult:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.error = None


def _is_self_disarming_manual_deployment(deployment: DeploymentManifest) -> bool:
    metadata = deployment.source.metadata or {}
    return (
        deployment.source.origin == "active_sheet_manual"
        and metadata.get("row_index") is not None
        and metadata.get("sheet_name") is not None
    )


def _hard_flat_market_fallback_due(
    exit_submitted_at: datetime | None,
    now: datetime,
    deployment: DeploymentManifest,
) -> bool:
    if exit_submitted_at is None:
        return False
    if now >= exit_submitted_at + timedelta(seconds=10):
        return True
    hard_flat_time = parse_time_text(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")
    now_seconds = as_et_time(now).hour * 3600 + as_et_time(now).minute * 60 + as_et_time(now).second
    hard_flat_seconds = hard_flat_time.hour * 3600 + hard_flat_time.minute * 60 + 30
    return now_seconds >= hard_flat_seconds


def _material_exit_price_change(previous_price: float | None, next_price: float | None) -> bool:
    if next_price is None:
        return False
    if previous_price is None:
        return True
    return round(previous_price, 2) != round(next_price, 2)


def _replace_position(position: TrackedPosition, **changes) -> TrackedPosition:
    return replace(position, **changes)
