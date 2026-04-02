"""Application bootstrap helpers."""

from __future__ import annotations

from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.app.runtime import BhikshaRuntime
from bhiksha.config.loader import (
    load_app_config,
    load_bias_config,
    load_provider_config,
    load_runtime_deployments,
    load_session_payload,
)
from bhiksha.strategy.registry import StrategyRegistry, default_strategy_registry


def build_runtime(
    config_root: str | Path = "config",
    *,
    session_payload_path: str | Path | None = None,
) -> BhikshaRuntime:
    """Build a runtime from config files without starting live services yet."""
    load_dotenv()
    config_root = Path(config_root)
    repo_root = config_root.parent
    app_config = load_app_config(config_root / "app.yaml")
    provider_config = load_provider_config(config_root / "providers.yaml")
    session_payload = None
    if session_payload_path is not None:
        payload_path = Path(session_payload_path)
        if not payload_path.is_absolute():
            payload_path = (repo_root / payload_path).resolve()
        session_payload = load_session_payload(payload_path)
        deployments = list(session_payload.deployments)
        deployment_selection = {
            "mode": "session_payload",
            "payload_path": str(payload_path),
            "session_id": session_payload.session_id,
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
        bias_inputs_path=bias_inputs_path,
        halt_and_flatten=bias_config.emergency.halt_and_flatten,
        strategy_registry=registry,
        deployment_selection=deployment_selection,
        session_payload=session_payload.model_dump(mode="json") if session_payload is not None else None,
    )
