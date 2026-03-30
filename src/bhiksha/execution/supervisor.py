"""Execution supervision for planned trades."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, time

from bhiksha.config.models import AppConfig
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import ExitDecision, ExitPlan, SignalDecision, TradePlan
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.market_data.session import as_et_time
from bhiksha.persistence.repository import EventRepository, NullEventRepository
from bhiksha.state.position_tracker import TrackedPosition


class ExecutionSupervisor:
    """Coordinates planning and event logging for a signal."""

    def __init__(
        self,
        planner: ExecutionPlanner | None = None,
        event_repository: EventRepository | None = None,
        app_config: AppConfig | None = None,
    ) -> None:
        self.planner = planner or ExecutionPlanner()
        self.event_repository = event_repository or NullEventRepository()
        self.app_config = app_config or AppConfig()

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
        plan = await self.planner.plan_entry(deployment, decision, dry_run=dry_run)
        if plan is not None:
            if not dry_run and plan.order_id:
                plan = await self._protect_live_entry(plan, deployment)
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
        if position.option_symbol is None or position.quantity <= 0:
            return None
        if position.entry_price is None:
            return None

        updated = position
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
            deployment.exit.stop_to_breakeven_after_r_multiple is not None
            and (updated.stop_price is None or updated.stop_price + 1e-9 < updated.entry_price)
        ):
            quote = await self.planner.order_manager.get_option_quote(updated.option_symbol)
            reference_price = quote.exit_reference_price
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

        if updated != position:
            self.planner.position_tracker.open_position(
                updated.symbol,
                updated.deployment_id,
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
        return updated

    def _supports_concurrent_exit_orders(self) -> bool:
        return bool(getattr(self.planner.order_manager, "supports_concurrent_exit_orders", False))

    async def handle_exit(
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
        if not decision.exit or decision.action == "hold" or position.option_symbol is None or position.quantity <= 0:
            return None

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
        plan = ExitPlan(
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
    ) -> list[TradePlan]:
        closed: list[TradePlan] = []
        now_et = as_et_time(now)
        for position in self.planner.position_tracker.active_positions():
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
            trade_plan = TradePlan(
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


def _parse_et_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _target_price(entry_price: float, stop_loss_pct: float, r_multiple: float) -> float:
    return entry_price * (1.0 + stop_loss_pct * r_multiple)


def _replace_position(position: TrackedPosition, **changes) -> TrackedPosition:
    return replace(position, **changes)
