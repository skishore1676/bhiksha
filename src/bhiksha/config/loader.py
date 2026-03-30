"""YAML-backed config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from bhiksha.config.models import AppConfig, DeploymentManifest, ProviderConfig

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
    manifests: list[DeploymentManifest] = []
    for file_path in sorted(root.glob("*.yaml")):
        manifests.append(_load_model(file_path, DeploymentManifest))
    return manifests

