"""Session-local, bounded Cartographer liquidity retry; never an order retry."""
from dataclasses import dataclass
from datetime import datetime
import math

from bhiksha.config.models import DeploymentManifest
from bhiksha.execution.cartographer_invalidation import entry_guard
from bhiksha.execution.planner import _entry_window_allows


@dataclass
class EntryLiquidityRetry:
    deployment: DeploymentManifest
    deadline: datetime
    next_attempt_at: datetime
    attempts: int = 1

    def stop_reason(self, now: datetime) -> str | None:
        if now >= self.deadline:
            return "signal_valid_no_trade_liquidity"
        if not _entry_window_allows(self.deployment, now):
            return "entry_window_closed"
        metadata = self.deployment.source.metadata
        valid_through = datetime.fromisoformat(str(metadata["valid_through"]).replace("Z", "+00:00"))
        if now >= valid_through:
            return "chart_signal_expired"
        return None

    def observation_reason(self, *, price: float, timestamp: datetime, now: datetime) -> str | None:
        if not math.isfinite(price) or price <= 0 or not 0 <= (now - timestamp).total_seconds() <= 5:
            return "entry_retry_observation_unusable"
        return entry_guard(self.deployment.source.metadata,
            direction=str(self.deployment.strategy.params["direction"]), close=price,
            observed_at=timestamp, now=now)
