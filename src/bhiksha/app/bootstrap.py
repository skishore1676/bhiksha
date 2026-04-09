"""Application bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.app.runtime import BhikshaRuntime
from bhiksha.config.loader import (
    load_app_config,
    load_active_plan,
    load_bias_config,
    load_provider_config,
    load_runtime_deployments,
    load_strategy_catalog,
)
from bhiksha.strategy.registry import StrategyRegistry, default_strategy_registry


def build_runtime(
    config_root: str | Path = "config",
    *,
    active_plan_path: str | Path | None = None,
) -> BhikshaRuntime:
    """Build a runtime from config files without starting live services yet."""
    load_dotenv()
    config_root = Path(config_root)
    repo_root = config_root.parent
    app_config = load_app_config(config_root / "app.yaml")
    provider_config = load_provider_config(config_root / "providers.yaml")
    strategy_catalog_path = Path(app_config.strategy_catalog_dir)
    if not strategy_catalog_path.is_absolute():
        strategy_catalog_path = repo_root / strategy_catalog_path
    strategy_catalog = load_strategy_catalog(strategy_catalog_path)
    active_plan = None
    if active_plan_path is not None:
        payload_path = Path(active_plan_path)
        if not payload_path.is_absolute():
            payload_path = (repo_root / payload_path).resolve()
        active_plan = load_active_plan(payload_path)
        deployments = list(active_plan.deployments)
        deployment_selection = {
            "mode": "active_plan",
            "active_plan_path": str(payload_path),
            "active_plan_id": active_plan.active_plan_id,
            "selected": [
                {
                    "deployment_id": manifest.deployment_id,
                    "symbol": manifest.symbol,
                    "source_kind": manifest.source_kind,
                    "enabled": manifest.enabled,
                }
                for manifest in deployments
            ],
            "skipped": [],
            "warnings": [],
        }
    else:
        deployments, deployment_selection = load_runtime_deployments(
            config_root / "deployments",
            generated_path=repo_root / app_config.generated_deployments_dir,
            selection_mode=app_config.deployment_selection_mode,
        )
    bias_inputs_path = Path(app_config.bias_inputs_path)
    if not bias_inputs_path.is_absolute():
        bias_inputs_path = repo_root / bias_inputs_path
    bias_config = load_bias_config(bias_inputs_path)
    registry: StrategyRegistry = default_strategy_registry()
    return BhikshaRuntime(
        app_config=app_config,
        provider_config=provider_config,
        deployments=deployments,
        bias_inputs=bias_config.selections,
        strategy_catalog=strategy_catalog,
        bias_inputs_path=bias_inputs_path,
        halt_and_flatten=bias_config.emergency.halt_and_flatten,
        strategy_registry=registry,
        deployment_selection=deployment_selection,
        active_plan=active_plan.model_dump(mode="json") if active_plan is not None else None,
    )
