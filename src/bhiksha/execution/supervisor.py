"""Execution supervision for planned trades."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, time
from datetime import UTC

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.config.models import AppConfig
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.events import ExitEvaluatedEvent, SignalEvaluatedEvent, TradeLifecycleTransitionEvent
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, ExitPlan, SignalDecision, TradePlan, TradeRecord
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.market_data.session import as_et_time
from bhiksha.persistence.repository import EventRepository, NullEventRepository, NullTradeStateRepository, TradeStateRepository
from bhiksha.state.lifecycle import LifecycleTransition, TradeLifecycleStore
from bhiksha.state.position_tracker import TrackedPosition


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
    ) -> None:
        self.planner = planner or ExecutionPlanner()
        self.event_repository = event_repository or NullEventRepository()
        self.trade_state_repository = trade_state_repository or NullTradeStateRepository()
        self.app_config = app_config or AppConfig()
        self.lifecycle_store = lifecycle_store or TradeLifecycleStore()
        self.event_bus = event_bus
        self._entry_lock = asyncio.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def close(self) -> None:
        await self.planner.close()

    async def handle_signal(
        self,
        deployment: DeploymentManifest,
        decision: SignalDecision,
        *,
        dry_run: bool,
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
                return None
            plan = await self.planner.plan_entry(deployment, decision, dry_run=dry_run)
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
                if not dry_run and plan.order_id:
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
            self.planner.position_tracker.close_position(
                deployment.symbol,
                deployment.deployment_id,
                option_symbol=plan.option_symbol,
                order_id=plan.order_id,
            )
            await self.trade_state_repository.mark_closed(plan.trade_id)
            transition = self.lifecycle_store.mark_closed(deployment.symbol, deployment.deployment_id)
            await self._emit_lifecycle_transition(transition, reason="entry_unfilled_closed")
            return plan

        stop_price = plan.estimated_entry_price * (1.0 - deployment.exit.stop_loss_pct)
        stop_result = await self.planner.order_manager.place_stop_loss_order(plan.option_symbol, stop_price, plan.quantity)
        target_order_id = None
        target_price = None
        if deployment.exit.use_profit_target and deployment.exit.profit_target_multiple is not None:
            target_price = _target_price(plan.estimated_entry_price, deployment.exit.stop_loss_pct, deployment.exit.profit_target_multiple)
            if self._supports_concurrent_exit_orders():
                target_result = await self.planner.order_manager.place_target_order(
                    plan.option_symbol,
                    target_price,
                    plan.quantity,
                )
                target_order_id = target_result.order_id
                await self.event_repository.append(
                    "profit_target_submission",
                    {
                        "deployment_id": plan.deployment_id,
                        "entry_order_id": plan.order_id,
                        "target_order_id": target_result.order_id,
                        "target_error": target_result.error,
                        "target_price": target_price,
                    },
                )
            else:
                await self.event_repository.append(
                    "profit_target_armed",
                    {
                        "deployment_id": plan.deployment_id,
                        "entry_order_id": plan.order_id,
                        "target_order_id": None,
                        "target_price": target_price,
                        "mode": "virtual",
                        "reason": "single_resting_exit_order_broker",
                    },
                )
        self.planner.position_tracker.open_position(
            deployment.symbol,
            deployment.deployment_id,
            trade_id=plan.trade_id,
            option_symbol=plan.option_symbol,
            quantity=plan.quantity,
            entry_price=plan.estimated_entry_price,
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
        await self.event_repository.append(
            "protective_stop_submission",
            {
                "deployment_id": plan.deployment_id,
                "entry_order_id": plan.order_id,
                "stop_order_id": stop_result.order_id,
                "stop_error": stop_result.error,
                "stop_price": stop_price,
            },
        )
        return replace(plan, stop_order_id=stop_result.order_id, target_order_id=target_order_id)

    async def manage_open_position(
        self,
        deployment: DeploymentManifest,
        position: TrackedPosition,
        *,
        dry_run: bool,
    ) -> TrackedPosition | None:
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

        updated = position
        quote = None

        async def ensure_quote():
            nonlocal quote
            if quote is None:
                quote = await self.planner.order_manager.get_option_quote(updated.option_symbol)
            return quote
        if deployment.exit.use_profit_target and deployment.exit.profit_target_multiple is not None and position.target_order_id is None:
            target_price = _target_price(position.entry_price, deployment.exit.stop_loss_pct, deployment.exit.profit_target_multiple)
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
                source=updated.source,
                order_id=updated.order_id,
                stop_order_id=updated.stop_order_id,
                stop_price=updated.stop_price,
                target_order_id=updated.target_order_id,
                target_price=updated.target_price,
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
                        status="target_active" if updated.target_order_id else "open_protected",
                        entry_order_id=updated.order_id,
                        stop_order_id=updated.stop_order_id,
                        stop_price=updated.stop_price,
                        target_order_id=updated.target_order_id,
                        target_price=updated.target_price,
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
        transition = self.lifecycle_store.mark_exit_pending(
            position.symbol,
            position.deployment_id,
            option_symbol=position.option_symbol,
            order_id=position.order_id,
        )
        await self._emit_lifecycle_transition(transition, reason="exit_submitted")

        canceled_stop_order_id = None
        canceled_target_order_id = None
        cancel_error = None
        if decision.cancel_protection_orders and position.stop_order_id:
            canceled, cancel_error = await self.planner.order_manager.cancel_order(position.stop_order_id)
            if canceled:
                canceled_stop_order_id = position.stop_order_id
            await self.event_repository.append(
                "protection_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "stop_order_id": position.stop_order_id,
                    "canceled": canceled,
                    "error": cancel_error,
                },
            )
        if decision.cancel_protection_orders and position.target_order_id:
            canceled, target_cancel_error = await self.planner.order_manager.cancel_order(position.target_order_id)
            if canceled:
                canceled_target_order_id = position.target_order_id
            if cancel_error is None:
                cancel_error = target_cancel_error
            await self.event_repository.append(
                "target_cancel_attempt",
                {
                    "deployment_id": deployment.deployment_id,
                    "symbol": position.symbol,
                    "option_symbol": position.option_symbol,
                    "target_order_id": position.target_order_id,
                    "canceled": canceled,
                    "error": target_cancel_error,
                },
            )

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
                canceled_stop_order_id=canceled_stop_order_id,
                canceled_target_order_id=canceled_target_order_id,
                error=f"unsupported_exit_action:{decision.action}",
            )

        order_id = "DRY_RUN_EXIT"
        error = cancel_error
        if not dry_run:
            result = await self.planner.order_manager.place_square_off_order(position.option_symbol, position.quantity)
            order_id = result.order_id
            error = result.error or error
            if result.order_id is None:
                await self.event_repository.append(
                    "exit_submission_failure",
                    {
                        "deployment_id": deployment.deployment_id,
                        "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "quantity": position.quantity,
                        "action": decision.action,
                        "error": error,
                    },
                )
                return ExitPlan(
                    trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                    deployment_id=deployment.deployment_id,
                    symbol=position.symbol,
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    action=decision.action,
                    reasons=decision.reason,
                    dry_run=False,
                    canceled_stop_order_id=canceled_stop_order_id,
                    canceled_target_order_id=canceled_target_order_id,
                    error=error,
                )

        self.planner.position_tracker.close_position(
            position.symbol,
            position.deployment_id,
            option_symbol=position.option_symbol,
        )
        if position.trade_id is not None:
            await self.trade_state_repository.mark_closed(position.trade_id, exit_order_id=order_id)
        transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
        await self._emit_lifecycle_transition(transition, reason="exit_closed")
        plan = ExitPlan(
            trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
            deployment_id=deployment.deployment_id,
            symbol=position.symbol,
            option_symbol=position.option_symbol,
            quantity=position.quantity,
            action=decision.action,
            reasons=decision.reason,
            dry_run=dry_run,
            order_id=order_id,
            canceled_stop_order_id=canceled_stop_order_id,
            canceled_target_order_id=canceled_target_order_id,
            error=error,
        )
        await self.event_repository.append("exit_plan", asdict(plan))
        return plan

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
            hard_flat_time = _parse_et_time(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")
            if now_et < hard_flat_time:
                continue

            order_id = "DRY_RUN_CLOSE"
            error = None
            if not dry_run:
                if position.stop_order_id:
                    canceled, cancel_error = await self.planner.order_manager.cancel_order(position.stop_order_id)
                    await self.event_repository.append(
                        "protection_cancel_attempt",
                        {
                            "deployment_id": position.deployment_id,
                            "symbol": position.symbol,
                            "option_symbol": position.option_symbol,
                            "stop_order_id": position.stop_order_id,
                            "canceled": canceled,
                            "error": cancel_error,
                        },
                    )
                if position.target_order_id:
                    canceled, cancel_error = await self.planner.order_manager.cancel_order(position.target_order_id)
                    await self.event_repository.append(
                        "target_cancel_attempt",
                        {
                            "deployment_id": position.deployment_id,
                            "symbol": position.symbol,
                            "option_symbol": position.option_symbol,
                            "target_order_id": position.target_order_id,
                            "canceled": canceled,
                            "error": cancel_error,
                        },
                    )
                result = await self.planner.order_manager.place_square_off_order(
                    position.option_symbol,
                    position.quantity,
                )
                order_id = result.order_id
                error = result.error
                if result.order_id is None:
                    await self.event_repository.append(
                        "hard_flat_failure",
                        {
                            "deployment_id": position.deployment_id,
                            "symbol": position.symbol,
                            "option_symbol": position.option_symbol,
                            "quantity": position.quantity,
                            "error": error,
                        },
                    )
                    continue

            self.planner.position_tracker.close_position(
                position.symbol,
                position.deployment_id,
                option_symbol=position.option_symbol,
            )
            if position.trade_id is not None:
                await self.trade_state_repository.mark_closed(position.trade_id, exit_order_id=order_id)
            transition = self.lifecycle_store.mark_closed(position.symbol, position.deployment_id)
            await self._emit_lifecycle_transition(transition, reason="hard_flat_closed")
            trade_plan = TradePlan(
                trade_id=position.trade_id or position.order_id or "UNKNOWN_TRADE",
                deployment_id=position.deployment_id,
                symbol=position.symbol,
                direction=SignalDirection(str(deployment.strategy.params.get("direction", "short")).lower()),
                option_symbol=position.option_symbol,
                quantity=position.quantity,
                estimated_entry_price=0.0,
                risk_reasons=["hard_flat_time_reached"],
                dry_run=dry_run,
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

    async def sync_lifecycle(self) -> None:
        transitions = self.lifecycle_store.sync_from_positions(self.planner.position_tracker.active_positions())
        for transition in transitions:
            await self._emit_lifecycle_transition(transition, reason="broker_reconciliation_sync")
        for position in self.planner.position_tracker.active_positions():
            if position.trade_id is None or position.option_symbol is None:
                continue
            await self._upsert_trade_record(
                TradeRecord(
                    trade_id=position.trade_id,
                    deployment_id=position.deployment_id,
                    symbol=position.symbol,
                    option_symbol=position.option_symbol,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    status="target_active" if position.target_order_id else "open_protected",
                    entry_order_id=position.order_id,
                    stop_order_id=position.stop_order_id,
                    stop_price=position.stop_price,
                    target_order_id=position.target_order_id,
                    target_price=position.target_price,
                )
            )

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

    async def _upsert_trade_record(self, record: TradeRecord) -> None:
        await self.trade_state_repository.upsert_trade(record)


def _parse_et_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _target_price(entry_price: float, stop_loss_pct: float, r_multiple: float) -> float:
    return entry_price * (1.0 + stop_loss_pct * r_multiple)


def _restore_threshold_price(entry_price: float, target_price: float, progress_pct: float) -> float:
    progress = max(0.0, min(1.0, progress_pct))
    return entry_price + ((target_price - entry_price) * progress)


def _replace_position(position: TrackedPosition, **changes) -> TrackedPosition:
    return replace(position, **changes)
