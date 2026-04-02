"""Application bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.app.runtime import BhikshaRuntime
from bhiksha.config.loader import load_app_config, load_bias_config, load_provider_config, load_runtime_deployments
from bhiksha.strategy.registry import StrategyRegistry, default_strategy_registry


def build_runtime(config_root: str | Path = "config") -> BhikshaRuntime:
    """Build a runtime from config files without starting live services yet."""
    load_dotenv()
    config_root = Path(config_root)
    repo_root = config_root.parent
    app_config = load_app_config(config_root / "app.yaml")
    provider_config = load_provider_config(config_root / "providers.yaml")
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
        bias_inputs_path=bias_inputs_path,
        halt_and_flatten=bias_config.emergency.halt_and_flatten,
        strategy_registry=registry,
        deployment_selection=deployment_selection,
    )
