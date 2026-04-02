"""YAML-backed config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

import json

from bhiksha.config.models import (
    AppConfig,
    BiasConfig,
    BiasSelection,
    DeploymentManifest,
    ProviderConfig,
    SessionPayload,
)

ConfigModelT = TypeVar("ConfigModelT", bound=BaseModel)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at {path}, found {type(data).__name__}")
    return data


def _load_model(path: Path, model_cls: type[ConfigModelT]) -> ConfigModelT:
    return model_cls.model_validate(_load_yaml(path))


def load_app_config(path: str | Path) -> AppConfig:
    return _load_model(Path(path), AppConfig)


def load_provider_config(path: str | Path) -> ProviderConfig:
    return _load_model(Path(path), ProviderConfig)


def load_deployments(path: str | Path) -> list[DeploymentManifest]:
    root = Path(path)
    if not root.exists():
        return []
    manifests: list[DeploymentManifest] = []
    seen_ids: dict[str, Path] = {}
    for file_path in sorted(root.rglob("*.yaml")):
        manifest = _load_model(file_path, DeploymentManifest)
        manifest.config_path = str(file_path.resolve())
        manifest.source_kind = "generated" if file_path.parent.name == "generated" or "generated" in file_path.parts else "manual"
        previous = seen_ids.get(manifest.deployment_id)
        if previous is not None:
            raise ValueError(
                f"Duplicate deployment_id {manifest.deployment_id!r} in {previous} and {file_path}"
            )
        seen_ids[manifest.deployment_id] = file_path
        manifests.append(manifest)
    return manifests


def load_session_payload(path: str | Path) -> SessionPayload:
    payload_path = Path(path)
    if not payload_path.exists():
        raise FileNotFoundError(payload_path)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    session = SessionPayload.model_validate(payload)
    seen_symbols: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for manifest in session.deployments:
        manifest.source_kind = "session"
        manifest.config_path = str(payload_path.resolve())
        previous_symbol = seen_symbols.get(manifest.symbol)
        if previous_symbol is not None:
            raise ValueError(
                f"Duplicate symbol {manifest.symbol!r} in session payload deployments "
                f"{previous_symbol!r} and {manifest.deployment_id!r}"
            )
        previous_id = seen_ids.get(manifest.deployment_id)
        if previous_id is not None:
            raise ValueError(
                f"Duplicate deployment_id {manifest.deployment_id!r} in session payload"
            )
        seen_symbols[manifest.symbol] = manifest.deployment_id
        seen_ids[manifest.deployment_id] = manifest.deployment_id
    return session


def load_runtime_deployments(
    path: str | Path,
    *,
    generated_path: str | Path | None = None,
    selection_mode: str = "all",
) -> tuple[list[DeploymentManifest], dict[str, Any]]:
    deployments = load_deployments(path)
    resolved_generated_path = Path(generated_path).resolve() if generated_path is not None else None
    for manifest in deployments:
        config_path = Path(manifest.config_path).resolve() if manifest.config_path else None
        if config_path is None or resolved_generated_path is None:
            continue
        manifest.source_kind = "generated" if config_path.is_relative_to(resolved_generated_path) else "manual"

    report: dict[str, Any] = {
        "mode": selection_mode,
        "selected": [],
        "skipped": [],
        "warnings": [],
    }
    if selection_mode == "all":
        selected = list(deployments)
    elif selection_mode == "manual_only":
        selected = _select_deployments(
            deployments,
            report,
            include=lambda manifest: manifest.source_kind != "generated",
            skip_reason="selection_mode_manual_only",
        )
    elif selection_mode == "generated_only":
        selected = _select_deployments(
            deployments,
            report,
            include=lambda manifest: manifest.source_kind == "generated",
            skip_reason="selection_mode_generated_only",
        )
    elif selection_mode == "prefer_generated":
        generated_symbols = {
            manifest.symbol
            for manifest in deployments
            if manifest.source_kind == "generated" and manifest.enabled
        }
        selected = []
        for manifest in deployments:
            if manifest.source_kind == "manual" and manifest.symbol in generated_symbols:
                report["skipped"].append(
                    {
                        "deployment_id": manifest.deployment_id,
                        "reason": "prefer_generated_symbol_override",
                        "symbol": manifest.symbol,
                        "source_kind": manifest.source_kind,
                    }
                )
                continue
            selected.append(manifest)
        duplicate_generated_symbols = sorted(
            symbol
            for symbol, count in _enabled_generated_counts_by_symbol(deployments).items()
            if count > 1
        )
        for symbol in duplicate_generated_symbols:
            report["warnings"].append(f"multiple_enabled_generated_deployments:{symbol}")
    else:
        raise ValueError(f"Unsupported deployment selection mode: {selection_mode}")

    report["selected"] = [
        {
            "deployment_id": manifest.deployment_id,
            "symbol": manifest.symbol,
            "source_kind": manifest.source_kind,
            "enabled": manifest.enabled,
        }
        for manifest in selected
    ]
    return selected, report


def load_bias_config(path: str | Path) -> BiasConfig:
    resolved = Path(path)
    if not resolved.exists():
        return BiasConfig()
    return _load_model(resolved, BiasConfig)


def load_bias_inputs(path: str | Path) -> list[BiasSelection]:
    return load_bias_config(path).selections


def _select_deployments(
    deployments: list[DeploymentManifest],
    report: dict[str, Any],
    *,
    include: Any,
    skip_reason: str,
) -> list[DeploymentManifest]:
    selected: list[DeploymentManifest] = []
    for manifest in deployments:
        if include(manifest):
            selected.append(manifest)
            continue
        report["skipped"].append(
            {
                "deployment_id": manifest.deployment_id,
                "reason": skip_reason,
                "symbol": manifest.symbol,
                "source_kind": manifest.source_kind,
            }
        )
    return selected


def _enabled_generated_counts_by_symbol(deployments: list[DeploymentManifest]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for manifest in deployments:
        if manifest.source_kind != "generated" or not manifest.enabled:
            continue
        counts[manifest.symbol] = counts.get(manifest.symbol, 0) + 1
    return counts
