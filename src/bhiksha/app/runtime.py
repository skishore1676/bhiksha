"""Runtime container for Bhiksha services."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import polars as pl

from bhiksha.app.event_bus import InMemoryEventBus
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.config.models import AppConfig, DeploymentManifest, ProviderConfig
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
from bhiksha.ops.health import check_polygon, check_public_auth, check_schwab_setup
from bhiksha.persistence.sqlite import SQLiteEventRepository
from bhiksha.state.reconciliation import reconcile_public_positions
from bhiksha.strategy.registry import StrategyRegistry


@dataclass(slots=True)
class BhikshaRuntime:
    """Thin runtime container for the initial scaffold."""

    app_config: AppConfig
    provider_config: ProviderConfig
    deployments: list[DeploymentManifest]
    strategy_registry: StrategyRegistry
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
        start = end - timedelta(days=self.app_config.warmup_trading_days + 3)

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
        supervisor = ExecutionSupervisor(
            event_repository=SQLiteEventRepository(self.app_config.sqlite_path),
            app_config=self.app_config,
        )
        position_monitor = PositionMonitor(evaluator, supervisor.planner.position_tracker)
        broker = supervisor.planner.order_manager.broker
        source = self._live_bar_source()
        daemon = DataIngestionDaemon(
            source,
            self.event_bus,
            symbols=symbols,
            provider=self.provider_config.underlying_live_primary,
        )
        queue = self.event_bus.subscribe(BarClosedEvent)

        try:
            portfolio = await broker.get_portfolio()
            tracker_positions = reconcile_public_positions(
                portfolio.get("positions", []),
                self.enabled_deployments,
                orders=portfolio.get("orders", []),
            )
            supervisor.planner.position_tracker.replace_positions(tracker_positions)
            supervisor.sync_lifecycle()
            output(f"SYNC positions={len(tracker_positions)}")

            for symbol in symbols:
                warmed = await self.warm_start_symbol(symbol)
                store.extend(symbol, warmed)
                output(f"WARMED {symbol} bars={len(warmed)}")

            if max_bars == 0:
                output("Stopping after warm start because --max-bars=0")
                return

            output("Waiting for newly closed 1-minute bars...")
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
                await self._handle_bar_event(
                    event,
                    live=live,
                    store=store,
                    broker=broker,
                    supervisor=supervisor,
                    position_monitor=position_monitor,
                    evaluator=evaluator,
                    deployments_by_symbol=deployments_by_symbol,
                    deployments_by_id=deployments_by_id,
                    output=output,
                )
                seen += 1
                if max_bars is not None and seen >= max_bars:
                    daemon.stop()
                    break
            daemon.stop()
            with suppress(asyncio.CancelledError):
                await daemon_task
        finally:
            await supervisor.close()
            await source.close()
            self.stop()

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
        live: bool,
        store: RollingBarStore,
        broker,
        supervisor: ExecutionSupervisor,
        position_monitor: PositionMonitor,
        evaluator: ReplaySignalEvaluator,
        deployments_by_symbol: dict[str, list[DeploymentManifest]],
        deployments_by_id: dict[str, DeploymentManifest],
        output: callable,
    ) -> None:
        bar = event.bar
        latest = store.latest(bar.symbol)
        if latest is not None and latest.timestamp >= bar.timestamp:
            return
        store.append(bar)
        output(f"BAR {bar.symbol} {bar.timestamp.isoformat()} close={bar.close}")

        portfolio = await broker.get_portfolio()
        tracker_positions = reconcile_public_positions(
            portfolio.get("positions", []),
            self.enabled_deployments,
            orders=portfolio.get("orders", []),
        )
        supervisor.planner.position_tracker.replace_positions(tracker_positions)
        supervisor.sync_lifecycle()
        if tracker_positions:
            joined = ",".join(
                f"{position.deployment_id}:{position.option_symbol}:{position.quantity}"
                for position in tracker_positions
            )
            output(f"SYNC positions={joined}")

        closed = await supervisor.close_due_positions(
            deployments_by_id,
            now=bar.timestamp,
            dry_run=not live,
        )
        for plan in closed:
            output(f"HARD_FLAT {plan.deployment_id} option={plan.option_symbol} order_id={plan.order_id}")

        frame = _frame_from_bars(bar.symbol, store.get(bar.symbol))
        for position in list(supervisor.planner.position_tracker.active_positions()):
            if position.symbol != bar.symbol:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None:
                continue
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

        exited_deployments: set[str] = set()
        exit_evaluations = position_monitor.evaluate_symbol(bar.symbol, frame, deployments_by_id)
        for evaluation in exit_evaluations:
            output(
                f"{evaluation.deployment.deployment_id}: exit={evaluation.decision.exit} "
                f"action={evaluation.decision.action} reasons={evaluation.decision.reason}"
            )
            if evaluation.decision.exit:
                exit_plan = await supervisor.handle_exit(
                    evaluation.deployment,
                    evaluation.position,
                    evaluation.decision,
                    dry_run=not live,
                )
                output(f"{evaluation.deployment.deployment_id}: exit_plan={exit_plan}")
                exited_deployments.add(evaluation.deployment.deployment_id)

        for deployment in deployments_by_symbol[bar.symbol]:
            if deployment.deployment_id in exited_deployments:
                output(f"{deployment.deployment_id}: entry_skipped_after_exit")
                continue
            decision = evaluator.evaluate_entry(deployment, frame)
            output(
                f"{deployment.deployment_id}: signal={decision.signal} "
                f"direction={decision.direction.value if decision.direction else None} "
                f"reasons={decision.reason}"
            )
            if decision.signal:
                plan = await supervisor.handle_signal(deployment, decision, dry_run=not live)
                output(f"{deployment.deployment_id}: plan={plan}")


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
