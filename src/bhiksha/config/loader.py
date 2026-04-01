"""YAML-backed config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from bhiksha.config.models import AppConfig, BiasConfig, BiasSelection, DeploymentManifest, ProviderConfig

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
        previous = seen_ids.get(manifest.deployment_id)
        if previous is not None:
            raise ValueError(
                f"Duplicate deployment_id {manifest.deployment_id!r} in {previous} and {file_path}"
            )
        seen_ids[manifest.deployment_id] = file_path
        manifests.append(manifest)
    return manifests


def load_bias_config(path: str | Path) -> BiasConfig:
    resolved = Path(path)
    if not resolved.exists():
        return BiasConfig()
    return _load_model(resolved, BiasConfig)


def load_bias_inputs(path: str | Path) -> list[BiasSelection]:
    return load_bias_config(path).selections
