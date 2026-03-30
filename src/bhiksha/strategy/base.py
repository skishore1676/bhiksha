"""Strategy interfaces used by the live runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from typing import Any, Protocol

import polars as pl

from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.state.position_tracker import TrackedPosition


class StrategyPlugin(Protocol):
    key: str

    def required_features(self, params: dict[str, Any]) -> set[str]:
        ...

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        ...

    def evaluate_exit(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        position: TrackedPosition,
    ) -> ExitDecision:
        ...


def coerce_time(value: time | str) -> time:
    """Accept either time objects or HH:MM strings."""
    if isinstance(value, time):
        return value
    return time.fromisoformat(value)


class DataFrameStrategyAdapter:
    """Bridge for research-era strategies that expose generate_signals(df)."""

    def __init__(
        self,
        key: str,
        required_features_fn: Callable[[dict[str, Any]], set[str]],
        generator: Callable[[pl.DataFrame, dict[str, Any]], pl.DataFrame],
    ) -> None:
        self.key = key
        self._required_features_fn = required_features_fn
        self._generator = generator

    def required_features(self, params: dict[str, Any]) -> set[str]:
        return self._required_features_fn(params)

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        enriched = self._generator(frame, params)
        latest = enriched.tail(1).to_dicts()
        if not latest:
            raise ValueError("Cannot evaluate strategy on an empty frame")
        row = latest[0]
        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(row["symbol"]),
            timestamp=row["timestamp"],
            signal=bool(row.get("signal", False)),
        )

    def evaluate_exit(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        position: TrackedPosition,
    ) -> ExitDecision:
        latest = frame.tail(1).to_dicts()
        if not latest:
            raise ValueError("Cannot evaluate strategy on an empty frame")
        row = latest[0]
        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(row["symbol"]),
            timestamp=row["timestamp"],
            exit=False,
            action="hold",
            reason=["exit_not_supported_by_adapter"],
        )
