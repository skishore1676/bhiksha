"""Session-local, bounded Cartographer liquidity retry; never an order retry."""
from dataclasses import dataclass
from datetime import datetime
import math

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import SignalDecision
from bhiksha.execution.cartographer_invalidation import entry_guard
from bhiksha.execution.planner import _entry_window_allows


@dataclass
class EntryLiquidityRetry:
    deployment: DeploymentManifest
    deadline: datetime
    next_attempt_at: datetime
    attempts: int = 1
    pending_decision: SignalDecision | None = None

    def stop_reason(self, now: datetime) -> str | None:
        if now >= self.deadline:
            return "liquidity_retry_deadline_reached"
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


def restore_consumed_retry_intents(events, deployments_by_id, supervisor) -> set[str]:
    """Restore consumed intent flags only; never replay an old signal or order."""
    from bhiksha.experiments.cartographer_attempts import OUTCOME_EVENT
    consumed = set()
    for event in events:
        payload = event.get("payload") or {}
        deployment_id = payload.get("deployment_id")
        deployment = deployments_by_id.get(deployment_id)
        if (event.get("event_type") == OUTCOME_EVENT
                and payload.get("reason") == "liquidity_retry_scheduled"
                and deployment is not None
                and payload.get("signal_id") == deployment.source.metadata.get("signal_id")):
            supervisor.consume_entry_intent(deployment_id)
            consumed.add(deployment_id)
    return consumed
