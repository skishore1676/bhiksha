"""Application bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.app.runtime import BhikshaRuntime
from bhiksha.config.loader import load_app_config, load_bias_inputs, load_deployments, load_provider_config
from bhiksha.strategy.registry import StrategyRegistry, default_strategy_registry


def build_runtime(config_root: str | Path = "config") -> BhikshaRuntime:
    """Build a runtime from config files without starting live services yet."""
    load_dotenv()
    config_root = Path(config_root)
    app_config = load_app_config(config_root / "app.yaml")
    provider_config = load_provider_config(config_root / "providers.yaml")
    deployments = load_deployments(config_root / "deployments")
    bias_inputs = load_bias_inputs(config_root / "bias_inputs.yaml")
    registry: StrategyRegistry = default_strategy_registry()
    return BhikshaRuntime(
        app_config=app_config,
        provider_config=provider_config,
        deployments=deployments,
        bias_inputs=bias_inputs,
        strategy_registry=registry,
    )
