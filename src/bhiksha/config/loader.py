"""YAML-backed config loading helpers."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

import json

from bhiksha.config.models import (
    ActivePlan,
    AppConfig,
    BiasConfig,
    BiasSelection,
    DeploymentManifest,
    ProviderConfig,
    StrategyCatalogEntry,
)

ConfigModelT = TypeVar("ConfigModelT", bound=BaseModel)
LEGACY_MALA_ORIGINS = {"mala", "mala_bias_translator_v1"}


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at {path}, found {type(data).__name__}")
    return data


def _load_model(path: Path, model_cls: type[ConfigModelT]) -> ConfigModelT:
    return model_cls.model_validate(_load_yaml(path))


def load_app_config(path: str | Path) -> AppConfig:
    payload = _load_yaml(Path(path))
    _apply_app_env_overrides(payload)
    return AppConfig.model_validate(payload)


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


def load_active_plan(path: str | Path) -> ActivePlan:
    payload_path = Path(path)
    if not payload_path.exists():
        raise FileNotFoundError(payload_path)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    plan = ActivePlan.model_validate(payload)
    seen_ids: dict[str, str] = {}
    for manifest in plan.deployments:
        manifest.source_kind = "active_plan"
        manifest.config_path = str(payload_path.resolve())
        previous_id = seen_ids.get(manifest.deployment_id)
        if previous_id is not None:
            raise ValueError(
                f"Duplicate deployment_id {manifest.deployment_id!r} in active plan"
            )
        seen_ids[manifest.deployment_id] = manifest.deployment_id
    return plan


def load_strategy_catalog(path: str | Path) -> list[StrategyCatalogEntry]:
    root = Path(path)
    if not root.exists():
        return []
    catalog: list[StrategyCatalogEntry] = []
    seen_ids: dict[str, Path] = {}
    for file_path in sorted(root.rglob("*.yaml")):
        entry = _load_model(file_path, StrategyCatalogEntry)
        entry.config_path = str(file_path.resolve())
        previous = seen_ids.get(entry.strategy_id)
        if previous is not None:
            raise ValueError(
                f"Duplicate strategy_id {entry.strategy_id!r} in {previous} and {file_path}"
            )
        seen_ids[entry.strategy_id] = file_path
        catalog.append(entry)
    return catalog


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

    selected = _suppress_legacy_runtime_wires(selected, report)

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


def _suppress_legacy_runtime_wires(
    deployments: list[DeploymentManifest],
    report: dict[str, Any],
) -> list[DeploymentManifest]:
    selected: list[DeploymentManifest] = []
    for manifest in deployments:
        if _is_legacy_runtime_wire(manifest):
            report["skipped"].append(
                {
                    "deployment_id": manifest.deployment_id,
                    "reason": "legacy_wire_retired",
                    "symbol": manifest.symbol,
                    "source_kind": manifest.source_kind,
                }
            )
            continue
        selected.append(manifest)
    return selected


def _is_legacy_runtime_wire(manifest: DeploymentManifest) -> bool:
    return bool(manifest.enabled and manifest.source.origin in LEGACY_MALA_ORIGINS)


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


def _apply_app_env_overrides(payload: dict[str, Any]) -> None:
    exit_edge_enabled = _env_bool("BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED")
    if exit_edge_enabled is not None:
        payload["exit_edge_live_shadow_enabled"] = exit_edge_enabled
    bool_value = _env_bool("BHIKSHA_ENTRY_REPRICE_ENABLED")
    if bool_value is not None:
        payload["entry_reprice_enabled"] = bool_value
    int_list = _env_int_list("BHIKSHA_ENTRY_REPRICE_CHECKPOINTS_SECONDS")
    if int_list is not None:
        payload["entry_reprice_checkpoints_seconds"] = int_list
    cancel_after = _env_int("BHIKSHA_ENTRY_REPRICE_CANCEL_AFTER_SECONDS")
    if cancel_after is not None:
        payload["entry_reprice_cancel_after_seconds"] = cancel_after
    float_list = _env_float_list("BHIKSHA_ENTRY_REPRICE_SPREAD_PCTS")
    if float_list is not None:
        payload["entry_reprice_spread_pcts"] = float_list


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


def _env_int_list(name: str) -> list[int] | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _env_float_list(name: str) -> list[float] | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return [float(part.strip()) for part in raw.split(",") if part.strip()]
