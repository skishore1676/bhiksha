"""Underlying invalidation authority for Cartographer-owned shadow positions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision
from bhiksha.market_data.session import ensure_utc
from bhiksha.state.position_tracker import TrackedPosition


def _metadata(deployment: DeploymentManifest) -> dict[str, Any] | None:
    metadata = deployment.source.metadata
    return metadata if metadata.get("source_owner") == "market_cartographer" else None


def entry_guard(
    metadata: dict[str, Any],
    *,
    direction: str,
    close: float,
    observed_at: datetime,
    now: datetime | None = None,
) -> str | None:
    """Return a truthful Cartographer entry-block reason, otherwise ``None``."""

    if metadata.get("source_owner") != "market_cartographer":
        return None
    observation = ensure_utc(observed_at)
    current = ensure_utc(now or datetime.now(UTC))
    if (current - observation).total_seconds() > 60:
        return "chart_entry_observation_stale"
    valid_through = datetime.fromisoformat(str(metadata["valid_through"]).replace("Z", "+00:00"))
    if observation > ensure_utc(valid_through):
        return "chart_signal_expired"
    invalidation = float(metadata["invalidation_price"])
    if direction == "long" and close <= invalidation:
        return "chart_invalidation_underlying"
    if direction == "short" and close >= invalidation:
        return "chart_invalidation_underlying"
    return None


def evaluate_invalidation(
    deployment: DeploymentManifest, frame: pl.DataFrame, position: TrackedPosition
) -> ExitDecision | None:
    """Yield a thesis-kill decision before any profile or native management."""

    metadata = _metadata(deployment)
    if metadata is None or frame.is_empty():
        return None
    latest = frame.tail(1).to_dicts()[0]
    close = float(latest["close"])
    invalidation = float(metadata["invalidation_price"])
    direction = str(deployment.strategy.params["direction"])
    breached = (direction == "long" and close <= invalidation) or (
        direction == "short" and close >= invalidation
    )
    if not breached:
        return None
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=deployment.symbol,
        timestamp=latest["timestamp"],
        exit=True,
        action="exit",
        reason=["chart_invalidation_underlying"],
        features={
            "underlying_close": close,
            "invalidation_price": invalidation,
            "profile_slug": metadata["profile_slug"],
            "signal_id": metadata["signal_id"],
            "position_option_symbol": position.option_symbol,
        },
    )


__all__ = ["entry_guard", "evaluate_invalidation"]
