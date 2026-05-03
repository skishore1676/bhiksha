"""Shared strategy capability metadata."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import yaml


NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS = frozenset({"manual_breakout", "market_impulse"})
DEFAULT_CAPABILITY_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "capabilities" / "bhiksha_capabilities_v1.yaml"
)
DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "artifacts" / "capabilities" / "bhiksha_runtime_capabilities_v2.json"
)
CAPABILITY_MANIFEST_ENV = "BHIKSHA_CAPABILITIES_PATH"

_MARKET_IMPULSE_NAME_VARIANTS = {
    "market impulse (cross & reclaim)": "cross_reclaim",
    "mi high close reclaim": "close_location_reclaim",
    "mi second touch": "delayed_reclaim",
    "mi shallow spring": "same_bar_shallow_reclaim",
    "mi push through": "continuation_confirmation",
}


@dataclass(frozen=True)
class StrategyCapability:
    strategy_key: str
    strategy_variant: str
    status: str
    reason: str
    manifest_version: int | None = None
    exit_policy_status: str = "not_checked"
    exit_policy_reason: str = ""

    @property
    def supported(self) -> bool:
        return self.status == "supported" and self.exit_policy_status in {"supported", "not_checked"}


def supports_native_algorithmic_exit(strategy_key: str | None) -> bool:
    """Return whether the strategy has a dedicated runtime-managed exit implementation."""
    normalized = (strategy_key or "").strip().lower()
    return normalized in NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS


def default_capability_manifest_path() -> Path:
    """Return the authoritative Bhiksha-owned capability manifest path."""
    configured = os.getenv(CAPABILITY_MANIFEST_ENV)
    if configured:
        return Path(configured).expanduser()
    if DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH.exists():
        return DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH
    return DEFAULT_CAPABILITY_MANIFEST_PATH


def load_capability_manifest(path: str | Path | None = None) -> dict[str, Any]:
    """Load the Bhiksha capability manifest."""
    resolved = Path(path).expanduser() if path is not None else default_capability_manifest_path()
    raw = resolved.read_text(encoding="utf-8")
    payload = json.loads(raw) if resolved.suffix.lower() == ".json" else yaml.safe_load(raw) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Capability manifest must be a mapping: {resolved}")
    if int(payload.get("version") or 0) < 1:
        raise ValueError(f"Capability manifest has missing or stale version: {resolved}")
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict):
        raise ValueError(f"Capability manifest missing strategies mapping: {resolved}")
    return payload


def load_capability_manifest_or_none(path: str | Path | None = None) -> dict[str, Any] | None:
    try:
        return load_capability_manifest(path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, yaml.YAMLError):
        return None


def derive_strategy_variant(
    *,
    strategy_key: str | None,
    strategy_name: str | None = None,
    strategy_params: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> str:
    """Derive the runtime variant from tested strategy evidence."""
    normalized_key = (strategy_key or "").strip().lower()
    params = strategy_params or {}
    if normalized_key == "market_impulse":
        entry_mode = str(params.get("entry_mode") or "").strip().lower()
        if entry_mode:
            return entry_mode
        name_variant = _MARKET_IMPULSE_NAME_VARIANTS.get((strategy_name or "").strip().lower())
        if name_variant:
            return name_variant
    if manifest:
        strategies = manifest.get("strategies")
        if isinstance(strategies, dict):
            strategy_config = strategies.get(normalized_key)
            if isinstance(strategy_config, dict):
                default_variant = strategy_config.get("default_variant")
                if default_variant:
                    return str(default_variant)
    return "default"


def evaluate_strategy_capability(
    *,
    strategy_key: str | None,
    strategy_variant: str | None = None,
    strategy_name: str | None = None,
    strategy_params: dict[str, Any] | None = None,
    thesis_exit_policy: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> StrategyCapability:
    """Evaluate whether a Mala strategy row is loadable by Bhiksha."""
    normalized_key = (strategy_key or "").strip().lower()
    active_manifest = manifest if manifest is not None else load_capability_manifest_or_none()
    variant = (
        (strategy_variant or "").strip()
        or derive_strategy_variant(
            strategy_key=normalized_key,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            manifest=active_manifest,
        )
    )
    if active_manifest is None:
        return StrategyCapability(
            strategy_key=normalized_key,
            strategy_variant=variant,
            status="unknown_manifest",
            reason="bhiksha_capability_manifest_missing",
        )

    version = int(active_manifest.get("version") or 0)
    strategies = active_manifest.get("strategies")
    strategy_config = strategies.get(normalized_key) if isinstance(strategies, dict) else None
    if not isinstance(strategy_config, dict):
        return StrategyCapability(
            strategy_key=normalized_key,
            strategy_variant=variant,
            status="unsupported",
            reason="strategy_key_not_in_bhiksha_capability_manifest",
            manifest_version=version,
        )

    variants = strategy_config.get("variants")
    variant_config = variants.get(variant) if isinstance(variants, dict) else None
    if not isinstance(variant_config, dict):
        return StrategyCapability(
            strategy_key=normalized_key,
            strategy_variant=variant,
            status="unsupported",
            reason="strategy_variant_not_in_bhiksha_capability_manifest",
            manifest_version=version,
        )

    status = str(variant_config.get("status") or "unsupported").strip().lower()
    reason = str(variant_config.get("reason") or "").strip()
    exit_policy_status = "not_checked"
    exit_policy_reason = ""
    if thesis_exit_policy:
        supported_policies = {
            str(policy).strip()
            for policy in (active_manifest.get("supported_thesis_exit_policies") or [])
            if str(policy).strip()
        }
        if thesis_exit_policy in supported_policies:
            exit_policy_status = "supported"
        else:
            exit_policy_status = "unsupported"
            exit_policy_reason = f"unsupported_thesis_exit_policy:{thesis_exit_policy}"
            status = "unsupported"
            reason = exit_policy_reason

    return StrategyCapability(
        strategy_key=normalized_key,
        strategy_variant=variant,
        status=status,
        reason=reason or ("supported" if status == "supported" else "unsupported"),
        manifest_version=version,
        exit_policy_status=exit_policy_status,
        exit_policy_reason=exit_policy_reason,
    )


__all__ = [
    "CAPABILITY_MANIFEST_ENV",
    "DEFAULT_CAPABILITY_MANIFEST_PATH",
    "DEFAULT_RUNTIME_CAPABILITY_MANIFEST_PATH",
    "NATIVE_ALGORITHMIC_EXIT_STRATEGY_KEYS",
    "StrategyCapability",
    "default_capability_manifest_path",
    "derive_strategy_variant",
    "evaluate_strategy_capability",
    "load_capability_manifest",
    "load_capability_manifest_or_none",
    "supports_native_algorithmic_exit",
]
