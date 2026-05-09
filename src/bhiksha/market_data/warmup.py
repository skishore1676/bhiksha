"""Warmup-window sizing for runtime feature parity."""

from __future__ import annotations

from math import ceil
from typing import Any, Iterable

from bhiksha.config.models import AppConfig, DeploymentManifest

LEGACY_RUNTIME_WARMUP_BUFFER_DAYS = 3
SESSION_MINUTES_PER_TRADING_DAY = 390


def legacy_effective_warmup_trading_days(app_config: AppConfig) -> int:
    """Return Bhiksha's historical fixed warmup window."""

    return max(int(app_config.warmup_trading_days), 0) + LEGACY_RUNTIME_WARMUP_BUFFER_DAYS


def effective_warmup_trading_days_for_deployment(
    app_config: AppConfig,
    deployment: DeploymentManifest,
) -> int:
    """Return the minimum runtime warmup days required for one deployment."""

    return max(
        legacy_effective_warmup_trading_days(app_config),
        required_warmup_trading_days_for_strategy(
            deployment.strategy.key,
            deployment.strategy.params,
        ),
    )


def effective_warmup_trading_days_for_deployments(
    app_config: AppConfig,
    deployments: Iterable[DeploymentManifest],
) -> int:
    """Return the runtime warmup days needed for a deployment collection."""

    days = legacy_effective_warmup_trading_days(app_config)
    for deployment in deployments:
        days = max(days, effective_warmup_trading_days_for_deployment(app_config, deployment))
    return days


def effective_warmup_trading_days_by_symbol(
    app_config: AppConfig,
    deployments: Iterable[DeploymentManifest],
) -> dict[str, int]:
    """Return symbol-level warmup days for active runtime preloading."""

    result: dict[str, int] = {}
    for deployment in deployments:
        result[deployment.symbol] = max(
            result.get(deployment.symbol, legacy_effective_warmup_trading_days(app_config)),
            effective_warmup_trading_days_for_deployment(app_config, deployment),
        )
    return result


def effective_warmup_trading_days_by_deployment(
    app_config: AppConfig,
    deployments: Iterable[DeploymentManifest],
) -> dict[str, int]:
    """Return deployment-level warmup days for startup snapshots and audits."""

    return {
        deployment.deployment_id: effective_warmup_trading_days_for_deployment(app_config, deployment)
        for deployment in deployments
    }


def required_warmup_trading_days_for_strategy(strategy_key: str, params: dict[str, Any]) -> int:
    """Estimate the feature-state lookback required by a strategy."""

    if strategy_key == "market_impulse":
        return _market_impulse_warmup_days(params)
    if strategy_key == "opening_drive_classifier":
        return _opening_drive_warmup_days(params)
    if strategy_key == "jerk_pivot_momentum":
        return _jerk_pivot_warmup_days(params)
    if strategy_key == "manual_breakout":
        return _manual_breakout_warmup_days(params)
    return 1


def _market_impulse_warmup_days(params: dict[str, Any]) -> int:
    timeframe_minutes = _timeframe_minutes(str(params.get("regime_timeframe", "1h")))
    vwma_periods = _coerce_ints(params.get("vwma_periods")) or [8, 21, 34]
    bars_needed = max(vwma_periods)
    return _bars_to_trading_days(bars_needed, timeframe_minutes) + 2


def _opening_drive_warmup_days(params: dict[str, Any]) -> int:
    days = 1
    if bool(params.get("use_regime_filter", False)):
        days = max(
            days,
            _market_impulse_warmup_days(
                {
                    "regime_timeframe": params.get("regime_timeframe", "5m"),
                    "vwma_periods": params.get("vwma_periods", [8, 21, 34]),
                }
            ),
        )
    return days


def _jerk_pivot_warmup_days(params: dict[str, Any]) -> int:
    volume_ma_period = int(params.get("volume_ma_period", 20))
    jerk_lookback = int(params.get("jerk_lookback", 20))
    kinematic_periods_back = max(int(params.get("kinematic_periods_back", 1)), 1)
    vpoc_minutes = int(params.get("vpoc_lookback_minutes", 240))
    bars_needed = max(vpoc_minutes, volume_ma_period, jerk_lookback + (3 * kinematic_periods_back))
    return _bars_to_trading_days(bars_needed, 1) + 1


def _manual_breakout_warmup_days(params: dict[str, Any]) -> int:
    timeframe_minutes = _timeframe_minutes(str(params.get("vma_timeframe", "5m")))
    bars_needed = int(params.get("vma_length", 10))
    return _bars_to_trading_days(bars_needed, timeframe_minutes) + 1


def _bars_to_trading_days(bars_needed: int, timeframe_minutes: int) -> int:
    minutes = max(int(bars_needed), 1) * max(int(timeframe_minutes), 1)
    return max(1, ceil(minutes / SESSION_MINUTES_PER_TRADING_DAY))


def _timeframe_minutes(timeframe: str) -> int:
    value = timeframe.strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    if value.endswith("h"):
        return int(value[:-1]) * 60
    return int(value)


def _coerce_ints(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(",", "_").split("_")]
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    result: list[int] = []
    for part in parts:
        if part in ("", None):
            continue
        result.append(int(part))
    return result
