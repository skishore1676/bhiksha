"""Runtime container for Bhiksha services."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import time

import polars as pl

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.app.execution_dispatcher import SymbolExecutionDispatcher
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.app.token_daemon import PublicTokenRefreshDaemon, SchwabTokenRefreshDaemon
from bhiksha.config.loader import load_bias_config
from bhiksha.config.models import AppConfig, BiasSelection, DeploymentManifest, ProviderConfig
from bhiksha.domain.events import BarClosedEvent
from bhiksha.domain.models import Bar
from bhiksha.domain.runtime import ProviderHealth, StartupReport
from bhiksha.execution.position_monitor import PositionMonitor
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.market_data.bar_store import RollingBarStore
from bhiksha.market_data.adapters.polygon import PolygonBarSource
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.market_data.daemon import DataIngestionDaemon
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.market_data.trading_calendar import trading_window_start
from bhiksha.ops.health import check_polygon, check_public_auth, check_schwab_setup
from bhiksha.ops.issues import classify_runtime_issue_category
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.persistence.sqlite import SQLiteTradeStateRepository
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.state.reconciliation import reconcile_public_positions
from bhiksha.strategy.registry import StrategyRegistry


@dataclass(slots=True)
class ReconciliationSnapshot:
    positions: list[TrackedPosition] = field(default_factory=list)
    last_synced_at: datetime | None = None
    signature: str = ""


@dataclass(slots=True)
class BhikshaRuntime:
    """Thin runtime container for the initial scaffold."""

    app_config: AppConfig
    provider_config: ProviderConfig
    deployments: list[DeploymentManifest]
    bias_inputs: list[BiasSelection]
    strategy_registry: StrategyRegistry
    bias_inputs_path: Path | None = None
    halt_and_flatten: bool = False
    started: bool = field(default=False, init=False)
    event_bus: InMemoryEventBus = field(default_factory=InMemoryEventBus, init=False)

    @property
    def enabled_deployments(self) -> list[DeploymentManifest]:
        return [deployment for deployment in self.deployments if deployment.enabled]

    def start(self) -> None:
        """Mark the runtime as started."""
        self.started = True

    def stop(self) -> None:
        """Mark the runtime as stopped."""
        self.started = False

    async def health_report(self) -> StartupReport:
        """Collect a dry-run startup health summary."""
        public_ok, public_detail = await check_public_auth()
        polygon_ok, polygon_detail = await check_polygon()
        schwab_ok, schwab_detail = await check_schwab_setup()
        return StartupReport(
            dry_run=self.app_config.dry_run,
            enabled_deployments=[deployment.deployment_id for deployment in self.enabled_deployments],
            provider_health=[
                ProviderHealth(name="public", ok=public_ok, detail=public_detail),
                ProviderHealth(name="polygon", ok=polygon_ok, detail=polygon_detail),
                ProviderHealth(name="schwab", ok=schwab_ok, detail=schwab_detail),
            ],
        )

    async def warm_start_symbol(self, symbol: str, *, provider: str | None = None) -> list[Bar]:
        """Warm start bars for a symbol using the configured provider."""
        provider = provider or self.provider_config.underlying_live_primary
        end = datetime.now(UTC)
        start = trading_window_start(end, self.app_config.warmup_trading_days + 3)

        if provider == "schwab":
            source = SchwabBarSource()
            try:
                return await source.warm_start(symbol, start, end)
            finally:
                await source.close()
        if provider == "polygon":
            source = PolygonBarSource()
            return await source.warm_start(symbol, start, end)
        raise ValueError(f"Unsupported warm-start provider: {provider}")

    async def run_session(
        self,
        *,
        live: bool,
        max_bars: int | None = None,
        output: callable = print,
    ) -> None:
        self.start()
        symbols = sorted({deployment.symbol for deployment in self.enabled_deployments})
        deployments_by_symbol = {symbol: [] for symbol in symbols}
        deployments_by_id = {}
        for deployment in self.enabled_deployments:
            deployments_by_symbol[deployment.symbol].append(deployment)
            deployments_by_id[deployment.deployment_id] = deployment

        store = RollingBarStore(max_bars_per_symbol=self.app_config.rolling_bar_capacity)
        evaluator = ReplaySignalEvaluator(FeatureService(), self.strategy_registry)
        event_repository = SQLiteEventRepository(self.app_config.sqlite_path)
        supervisor = ExecutionSupervisor(
            event_repository=event_repository,
            app_config=self.app_config,
            event_bus=self.event_bus,
            trade_state_repository=SQLiteTradeStateRepository(self.app_config.sqlite_path),
        )
        position_monitor = PositionMonitor(evaluator, supervisor.planner.position_tracker)
        broker = supervisor.planner.order_manager.broker
        source = self._live_bar_source()
        public_token_daemon = PublicTokenRefreshDaemon()
        schwab_token_daemon = SchwabTokenRefreshDaemon()
        daemon = DataIngestionDaemon(
            source,
            self.event_bus,
            symbols=symbols,
            provider=self.provider_config.underlying_live_primary,
        )
        execution_dispatcher = SymbolExecutionDispatcher()
        execution_dispatcher.start(symbols)
        queue = self.event_bus.subscribe(BarClosedEvent)
        sync_lock = asyncio.Lock()
        reconcile_trigger = asyncio.Event()
        stop_event = asyncio.Event()
        reconciliation_snapshot = ReconciliationSnapshot()
        symbol_queues = {symbol: asyncio.Queue() for symbol in symbols}
        symbol_tasks = [
            asyncio.create_task(
                self._symbol_worker(
                    symbol,
                    symbol_queues[symbol],
                    sync_lock=sync_lock,
                    reconciliation_snapshot=reconciliation_snapshot,
                    reconcile_trigger=reconcile_trigger,
                    live=live,
                    store=store,
                    supervisor=supervisor,
                    position_monitor=position_monitor,
                    evaluator=evaluator,
                    execution_dispatcher=execution_dispatcher,
                    deployments_by_symbol=deployments_by_symbol,
                    deployments_by_id=deployments_by_id,
                    output=output,
                )
            )
            for symbol in symbols
        ]

        try:
            startup_snapshot = self.startup_snapshot(live=live, max_bars=max_bars)
            output("STARTUP_CONFIG " + json.dumps(startup_snapshot, sort_keys=True))
            await event_repository.append("startup_config", startup_snapshot)
            await self._refresh_reconciliation(
                broker=broker,
                supervisor=supervisor,
                sync_lock=sync_lock,
                reconciliation_snapshot=reconciliation_snapshot,
                output=output,
                reason="startup",
            )

            for symbol in symbols:
                warmed = await self.warm_start_symbol(symbol)
                store.extend(symbol, warmed)
                output(f"WARMED {symbol} bars={len(warmed)}")

            if max_bars == 0:
                output("Stopping after warm start because --max-bars=0")
                return

            output("Waiting for newly closed 1-minute bars...")
            public_token_task = asyncio.create_task(public_token_daemon.run())
            schwab_token_task = asyncio.create_task(schwab_token_daemon.run())
            reconciliation_task = asyncio.create_task(
                self._reconciliation_loop(
                    broker=broker,
                    supervisor=supervisor,
                    sync_lock=sync_lock,
                    reconciliation_snapshot=reconciliation_snapshot,
                    reconcile_trigger=reconcile_trigger,
                    stop_event=stop_event,
                    output=output,
                )
            )
            daemon_task = asyncio.create_task(daemon.run(max_bars=max_bars))
            seen = 0
            while True:
                if daemon_task.done() and queue.empty():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if daemon_task.done():
                        break
                    continue
                await symbol_queues[event.symbol].put(event)
                seen += 1
                if max_bars is not None and seen >= max_bars:
                    daemon.stop()
                    break
            daemon.stop()
            for queue_ in symbol_queues.values():
                await queue_.put(None)
            public_token_daemon.stop()
            schwab_token_daemon.stop()
            stop_event.set()
            reconcile_trigger.set()
            with suppress(asyncio.CancelledError):
                await daemon_task
            with suppress(asyncio.CancelledError):
                await public_token_task
            with suppress(asyncio.CancelledError):
                await schwab_token_task
            with suppress(asyncio.CancelledError):
                await reconciliation_task
            for task in symbol_tasks:
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            public_token_daemon.stop()
            schwab_token_daemon.stop()
            stop_event.set()
            reconcile_trigger.set()
            await execution_dispatcher.stop()
            await supervisor.close()
            await source.close()
            self.stop()

    def startup_snapshot(self, *, live: bool, max_bars: int | None) -> dict:
        payload = {
            "app": self.app_config.model_dump(),
            "providers": self.provider_config.model_dump(),
            "deployments": [deployment.model_dump() for deployment in self.enabled_deployments],
            "bias_inputs": [selection.model_dump() for selection in self.bias_inputs],
            "emergency_controls": {
                "halt_and_flatten": self.halt_and_flatten,
            },
            "session": {
                "live": live,
                "max_bars": max_bars,
            },
        }
        payload["config_fingerprint"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return payload

    async def _symbol_worker(
        self,
        symbol: str,
        queue: asyncio.Queue,
        *,
        sync_lock: asyncio.Lock,
        reconciliation_snapshot: ReconciliationSnapshot,
        reconcile_trigger: asyncio.Event,
        live: bool,
        store: RollingBarStore,
        supervisor: ExecutionSupervisor,
        position_monitor: PositionMonitor,
        evaluator: ReplaySignalEvaluator,
        execution_dispatcher: SymbolExecutionDispatcher,
        deployments_by_symbol: dict[str, list[DeploymentManifest]],
        deployments_by_id: dict[str, DeploymentManifest],
        output: callable,
    ) -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            try:
                await self._handle_bar_event(
                    event,
                    sync_lock=sync_lock,
                    reconciliation_snapshot=reconciliation_snapshot,
                    reconcile_trigger=reconcile_trigger,
                    live=live,
                    store=store,
                    supervisor=supervisor,
                    position_monitor=position_monitor,
                    evaluator=evaluator,
                    execution_dispatcher=execution_dispatcher,
                    deployments_by_symbol=deployments_by_symbol,
                    deployments_by_id=deployments_by_id,
                    output=output,
                )
            except Exception as exc:
                await supervisor.event_repository.append(
                    "runtime_issue",
                    {
                        "category": classify_runtime_issue_category(error=str(exc), event_type="symbol_worker"),
                        "symbol": symbol,
                        "error": str(exc),
                        "stage": "symbol_worker",
                    },
                )
                output(f"RUNTIME_ISSUE {symbol} stage=symbol_worker error={exc}")

    def _live_bar_source(self):
        provider = self.provider_config.underlying_live_primary
        if provider == "schwab":
            return SchwabBarSource(poll_interval_seconds=self.app_config.bar_poll_interval_seconds)
        if provider == "polygon":
            return PolygonBarSource()
        raise ValueError(f"Unsupported live provider: {provider}")

    async def _handle_bar_event(
        self,
        event: BarClosedEvent,
        *,
        sync_lock: asyncio.Lock,
        reconciliation_snapshot: ReconciliationSnapshot,
        reconcile_trigger: asyncio.Event,
        live: bool,
        store: RollingBarStore,
        supervisor: ExecutionSupervisor,
        position_monitor: PositionMonitor,
        evaluator: ReplaySignalEvaluator,
        execution_dispatcher: SymbolExecutionDispatcher,
        deployments_by_symbol: dict[str, list[DeploymentManifest]],
        deployments_by_id: dict[str, DeploymentManifest],
        output: callable,
    ) -> None:
        bar = event.bar
        processing_started_at = time.perf_counter()
        latest = store.latest(bar.symbol)
        if latest is not None and latest.timestamp >= bar.timestamp:
            return
        store.append(bar)
        output(f"BAR {bar.symbol} {bar.timestamp.isoformat()} close={bar.close}")

        expected_heartbeat_at = bar.timestamp + timedelta(minutes=1, seconds=1)
        heartbeat_lag_ms = max((datetime.now(UTC) - expected_heartbeat_at).total_seconds() * 1000.0, 0.0)
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "heartbeat_lag_ms",
                "symbol": bar.symbol,
                "value": round(heartbeat_lag_ms, 3),
                "unit": "ms",
            },
        )

        emergency_changed = await self._refresh_intraday_bias_controls(supervisor=supervisor, output=output)
        if emergency_changed or self.halt_and_flatten:
            output(f"EMERGENCY_STATE halt_and_flatten={self.halt_and_flatten}")

        async with sync_lock:
            tracker_positions = list(reconciliation_snapshot.positions)
            last_synced_at = reconciliation_snapshot.last_synced_at
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "reconciliation_staleness_ms",
                "symbol": bar.symbol,
                "value": round(
                    max(((datetime.now(UTC) - last_synced_at).total_seconds() * 1000.0), 0.0) if last_synced_at else 0.0,
                    3,
                ),
                "unit": "ms",
            },
        )
        if tracker_positions:
            joined = ",".join(
                f"{position.deployment_id}:{position.option_symbol}:{position.quantity}"
                for position in tracker_positions
            )
            output(f"SYNC positions={joined}")

        if self.halt_and_flatten:
            emergency_enqueued = execution_dispatcher.submit(
                bar.symbol,
                key=f"emergency_flat:{bar.symbol}",
                runner=self._instrument_execution_runner(
                    supervisor,
                    bar.symbol,
                    deployment_id=None,
                    reconcile_trigger=reconcile_trigger,
                    action="emergency_flat",
                    inner=self._make_emergency_flat_runner(
                        supervisor,
                        deployments_by_id,
                        live=live,
                        symbol=bar.symbol,
                        output=output,
                    ),
                ),
            )
            if emergency_enqueued:
                output(f"EXECUTION_ENQUEUED {bar.symbol} emergency_flat")
            await supervisor.event_repository.append(
                "runtime_metric",
                {
                    "metric": "processing_ms",
                    "symbol": bar.symbol,
                    "value": round((time.perf_counter() - processing_started_at) * 1000.0, 3),
                    "unit": "ms",
                },
            )
            return

        hard_flat_enqueued = execution_dispatcher.submit(
            bar.symbol,
            key=f"hard_flat:{bar.symbol}",
            runner=self._instrument_execution_runner(
                supervisor,
                bar.symbol,
                deployment_id=None,
                reconcile_trigger=reconcile_trigger,
                action="hard_flat",
                inner=self._make_hard_flat_runner(
                supervisor,
                deployments_by_id,
                now=bar.timestamp,
                live=live,
                symbol=bar.symbol,
                output=output,
                ),
            ),
        )
        if hard_flat_enqueued:
            output(f"EXECUTION_ENQUEUED {bar.symbol} hard_flat_check")

        frame = _frame_from_bars(bar.symbol, store.get(bar.symbol))
        deployments_for_bar: dict[str, DeploymentManifest] = {
            deployment.deployment_id: deployment for deployment in deployments_by_symbol[bar.symbol]
        }
        for position in tracker_positions:
            if position.symbol != bar.symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is not None:
                deployments_for_bar[deployment.deployment_id] = deployment
        feature_started_at = time.perf_counter()
        enriched_frames = evaluator.prepare_enriched_frames(frame, list(deployments_for_bar.values()))
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "feature_prep_ms",
                "symbol": bar.symbol,
                "value": round((time.perf_counter() - feature_started_at) * 1000.0, 3),
                "unit": "ms",
            },
        )
        for position in list(tracker_positions):
            if position.symbol != bar.symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None:
                continue
            enqueued = execution_dispatcher.submit(
                bar.symbol,
                key=f"manage:{deployment.deployment_id}:{position.option_symbol}",
                runner=self._instrument_execution_runner(
                    supervisor,
                    bar.symbol,
                    deployment_id=deployment.deployment_id,
                    reconcile_trigger=reconcile_trigger,
                    action="manage",
                    inner=self._make_manage_position_runner(
                    supervisor,
                    deployment,
                    position,
                    live=live,
                    output=output,
                    ),
                ),
            )
            if enqueued:
                output(f"{deployment.deployment_id}: manage_enqueued option={position.option_symbol}")

        exited_deployments: set[str] = set()
        exit_evaluations = position_monitor.evaluate_symbol(
            bar.symbol,
            frame,
            deployments_by_id,
            enriched_frames=enriched_frames,
            positions=tracker_positions,
        )
        for evaluation in exit_evaluations:
            output(
                f"{evaluation.deployment.deployment_id}: exit={evaluation.decision.exit} "
                f"action={evaluation.decision.action} reasons={evaluation.decision.reason}"
            )
            if evaluation.decision.exit:
                enqueued = execution_dispatcher.submit(
                    bar.symbol,
                    key=f"exit:{evaluation.deployment.deployment_id}:{evaluation.position.option_symbol}",
                    runner=self._instrument_execution_runner(
                        supervisor,
                        bar.symbol,
                        deployment_id=evaluation.deployment.deployment_id,
                        reconcile_trigger=reconcile_trigger,
                        action="exit",
                        inner=self._make_exit_runner(
                        supervisor,
                        evaluation.deployment,
                        evaluation.position,
                        evaluation.decision,
                        live=live,
                        output=output,
                        ),
                    ),
                )
                if enqueued:
                    output(f"{evaluation.deployment.deployment_id}: exit_enqueued option={evaluation.position.option_symbol}")
                exited_deployments.add(evaluation.deployment.deployment_id)

        for deployment in deployments_by_symbol[bar.symbol]:
            if deployment.deployment_id in exited_deployments:
                output(f"{deployment.deployment_id}: entry_skipped_after_exit")
                continue
            enriched = enriched_frames.get(deployment.deployment_id)
            if enriched is not None:
                decision = evaluator.evaluate_entry_on_enriched(deployment, enriched)
            else:
                decision = evaluator.evaluate_entry(deployment, frame)
            output(
                f"{deployment.deployment_id}: signal={decision.signal} "
                f"direction={decision.direction.value if decision.direction else None} "
                f"reasons={decision.reason}"
            )
            if decision.signal:
                enqueued = execution_dispatcher.submit(
                    bar.symbol,
                    key=f"entry:{deployment.deployment_id}",
                    runner=self._instrument_execution_runner(
                        supervisor,
                        bar.symbol,
                        deployment_id=deployment.deployment_id,
                        reconcile_trigger=reconcile_trigger,
                        action="entry",
                        inner=self._make_entry_runner(
                        supervisor,
                        deployment,
                        decision,
                        live=live,
                        output=output,
                        ),
                    ),
                )
                if enqueued:
                    output(f"{deployment.deployment_id}: entry_enqueued")
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "execution_queue_depth",
                "symbol": bar.symbol,
                "value": execution_dispatcher.queue_depth(bar.symbol),
                "unit": "count",
            },
        )
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "execution_pending_count",
                "symbol": bar.symbol,
                "value": execution_dispatcher.pending_count(bar.symbol),
                "unit": "count",
            },
        )
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "processing_ms",
                "symbol": bar.symbol,
                "value": round((time.perf_counter() - processing_started_at) * 1000.0, 3),
                "unit": "ms",
            },
        )

    def _instrument_execution_runner(
        self,
        supervisor: ExecutionSupervisor,
        symbol: str,
        *,
        deployment_id: str | None,
        reconcile_trigger: asyncio.Event,
        action: str,
        inner,
    ):
        queued_at = time.perf_counter()

        async def runner() -> None:
            started_at = time.perf_counter()
            await supervisor.event_repository.append(
                "runtime_metric",
                {
                    "metric": "execution_wait_ms",
                    "symbol": symbol,
                    "action": action,
                    "value": round((started_at - queued_at) * 1000.0, 3),
                    "unit": "ms",
                },
            )
            try:
                await inner()
            except Exception as exc:
                await supervisor.event_repository.append(
                    "runtime_issue",
                    {
                        "category": classify_runtime_issue_category(error=str(exc), event_type=action),
                        "deployment_id": deployment_id,
                        "symbol": symbol,
                        "action": action,
                        "error": str(exc),
                        "stage": "execution_runner",
                    },
                )
                output(f"RUNTIME_ISSUE {symbol} action={action} error={exc}")
            finally:
                reconcile_trigger.set()
                await supervisor.event_repository.append(
                    "runtime_metric",
                    {
                        "metric": "execution_run_ms",
                        "symbol": symbol,
                        "action": action,
                        "value": round((time.perf_counter() - started_at) * 1000.0, 3),
                        "unit": "ms",
                    },
                )

        return runner

    async def _refresh_intraday_bias_controls(
        self,
        *,
        supervisor: ExecutionSupervisor,
        output: callable,
    ) -> bool:
        if self.bias_inputs_path is None:
            return False
        try:
            bias_config = load_bias_config(self.bias_inputs_path)
        except Exception as exc:
            await supervisor.event_repository.append(
                "runtime_issue",
                {
                    "category": classify_runtime_issue_category(error=str(exc), event_type="bias_inputs"),
                    "symbol": "ALL",
                    "error": str(exc),
                    "stage": "bias_reload",
                },
            )
            output(f"RUNTIME_ISSUE ALL stage=bias_reload error={exc}")
            return False
        changed = bias_config.emergency.halt_and_flatten != self.halt_and_flatten
        self.halt_and_flatten = bias_config.emergency.halt_and_flatten
        if changed:
            await supervisor.event_repository.append(
                "emergency_control_update",
                {
                    "halt_and_flatten": self.halt_and_flatten,
                    "source": str(self.bias_inputs_path),
                },
            )
        return changed

    async def _reconciliation_loop(
        self,
        *,
        broker,
        supervisor: ExecutionSupervisor,
        sync_lock: asyncio.Lock,
        reconciliation_snapshot: ReconciliationSnapshot,
        reconcile_trigger: asyncio.Event,
        stop_event: asyncio.Event,
        output: callable,
    ) -> None:
        interval = max(self.app_config.reconciliation_interval_seconds, 1)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(reconcile_trigger.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            reconcile_trigger.clear()
            if stop_event.is_set():
                return
            await self._refresh_reconciliation(
                broker=broker,
                supervisor=supervisor,
                sync_lock=sync_lock,
                reconciliation_snapshot=reconciliation_snapshot,
                output=output,
                reason="periodic",
            )

    async def _refresh_reconciliation(
        self,
        *,
        broker,
        supervisor: ExecutionSupervisor,
        sync_lock: asyncio.Lock,
        reconciliation_snapshot: ReconciliationSnapshot,
        output: callable,
        reason: str,
    ) -> None:
        sync_started_at = time.perf_counter()
        try:
            portfolio = await broker.get_portfolio()
            open_trades = await supervisor.trade_state_repository.get_open_trades()
            tracker_positions = reconcile_public_positions(
                portfolio.get("positions", []),
                self.enabled_deployments,
                orders=portfolio.get("orders", []),
                known_trades=open_trades,
            )
        except Exception as exc:
            await supervisor.event_repository.append(
                "runtime_issue",
                {
                    "category": classify_runtime_issue_category(error=str(exc), event_type="reconciliation"),
                    "symbol": "ALL",
                    "error": str(exc),
                    "stage": "reconciliation",
                    "reason": reason,
                },
            )
            output(f"RUNTIME_ISSUE ALL stage=reconciliation reason={reason} error={exc}")
            return
        async with sync_lock:
            supervisor.planner.position_tracker.replace_positions(tracker_positions)
            await supervisor.sync_lifecycle()
            reconciliation_snapshot.positions = list(tracker_positions)
            reconciliation_snapshot.last_synced_at = datetime.now(UTC)
            reconciliation_snapshot.signature = ",".join(
                f"{position.deployment_id}:{position.option_symbol}:{position.quantity}"
                for position in tracker_positions
            )
        await supervisor.event_repository.append(
            "runtime_metric",
            {
                "metric": "portfolio_sync_ms",
                "symbol": "ALL",
                "value": round((time.perf_counter() - sync_started_at) * 1000.0, 3),
                "unit": "ms",
                "reason": reason,
            },
        )
        if reason == "startup" or tracker_positions:
            output(f"SYNC positions={reconciliation_snapshot.signature or len(tracker_positions)}")

    def _make_entry_runner(
        self,
        supervisor: ExecutionSupervisor,
        deployment: DeploymentManifest,
        decision,
        *,
        live: bool,
        output: callable,
    ):
        async def runner() -> None:
            simulate_only = deployment.execution.shadow_only
            plan = await supervisor.handle_signal(
                deployment,
                decision,
                dry_run=(not live) or simulate_only,
                simulate_only=simulate_only,
            )
            if simulate_only:
                output(f"{deployment.deployment_id}: shadow_plan={plan}")
            else:
                output(f"{deployment.deployment_id}: plan={plan}")

        return runner

    def _make_exit_runner(
        self,
        supervisor: ExecutionSupervisor,
        deployment: DeploymentManifest,
        position,
        decision,
        *,
        live: bool,
        output: callable,
    ):
        async def runner() -> None:
            exit_plan = await supervisor.handle_exit(
                deployment,
                position,
                decision,
                dry_run=not live,
            )
            output(f"{deployment.deployment_id}: exit_plan={exit_plan}")

        return runner

    def _make_manage_position_runner(
        self,
        supervisor: ExecutionSupervisor,
        deployment: DeploymentManifest,
        position,
        *,
        live: bool,
        output: callable,
    ):
        async def runner() -> None:
            managed = await supervisor.manage_open_position(
                deployment,
                position,
                dry_run=not live,
            )
            if managed is not None and managed != position:
                output(
                    f"{deployment.deployment_id}: position_managed "
                    f"stop={managed.stop_order_id}@{managed.stop_price} "
                    f"target={managed.target_order_id}@{managed.target_price}"
                )

        return runner

    def _make_hard_flat_runner(
        self,
        supervisor: ExecutionSupervisor,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        now: datetime,
        live: bool,
        symbol: str,
        output: callable,
    ):
        async def runner() -> None:
            closed = await supervisor.close_due_positions(
                deployments_by_id,
                now=now,
                dry_run=not live,
                symbol=symbol,
            )
            for plan in closed:
                output(f"HARD_FLAT {plan.deployment_id} option={plan.option_symbol} order_id={plan.order_id}")

        return runner

    def _make_emergency_flat_runner(
        self,
        supervisor: ExecutionSupervisor,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        live: bool,
        symbol: str,
        output: callable,
    ):
        async def runner() -> None:
            closed = await supervisor.halt_and_flatten_positions(
                deployments_by_id,
                dry_run=not live,
                symbol=symbol,
            )
            for plan in closed:
                output(f"EMERGENCY_FLAT {plan.deployment_id} option={plan.option_symbol} order_id={plan.order_id}")

        return runner


def _frame_from_bars(symbol: str, bars) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol for _ in bars],
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )
