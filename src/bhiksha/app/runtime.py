"""Runtime container for Bhiksha services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bhiksha.config.models import AppConfig, DeploymentManifest, ProviderConfig
from bhiksha.domain.models import Bar
from bhiksha.domain.runtime import ProviderHealth, StartupReport
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.market_data.adapters.polygon import PolygonBarSource
from bhiksha.market_data.adapters.schwab import SchwabBarSource
from bhiksha.ops.health import check_polygon, check_public_auth, check_schwab_setup
from bhiksha.strategy.registry import StrategyRegistry


@dataclass(slots=True)
class BhikshaRuntime:
    """Thin runtime container for the initial scaffold."""

    app_config: AppConfig
    provider_config: ProviderConfig
    deployments: list[DeploymentManifest]
    strategy_registry: StrategyRegistry
    started: bool = field(default=False, init=False)

    @property
    def enabled_deployments(self) -> list[DeploymentManifest]:
        return [deployment for deployment in self.deployments if deployment.enabled]

    def start(self) -> None:
        """Mark the runtime as started.

        The live loops are intentionally not wired yet; this scaffold gives us
        a stable place to assemble services before market-data and broker
        integrations land.
        """
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
