"""Execution ownership. Native groups remain unavailable pending lifecycle proof."""
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import TradePlan

ROUTE_SIMPLE_ENTRY = "simple_entry_with_broker_protection"
ROUTE_APPLICATION_MANAGED = "application_managed_exit"


def decide_execution_route(deployment: DeploymentManifest, plan: TradePlan | None = None) -> str:
    # Check here as well: Pydantic model_copy does not validate updates.
    if deployment.execution.enable_native_bracket_route or deployment.execution.enable_native_oto_route:
        raise ValueError("native order groups require child reconciliation and restart recovery")
    if deployment.exit.profile_exit_id:
        return ROUTE_APPLICATION_MANAGED
    return ROUTE_SIMPLE_ENTRY
