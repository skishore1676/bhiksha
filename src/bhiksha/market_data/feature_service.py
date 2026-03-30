"""Feature enrichment service for replay and live evaluation."""

from __future__ import annotations

import polars as pl

from bhiksha.market_data.newton.engine import PhysicsEngine


class FeatureService:
    """Thin wrapper around the Newton physics engine."""

    def __init__(self, engine: PhysicsEngine | None = None) -> None:
        self.engine = engine or PhysicsEngine()

    def enrich_for_features(self, df: pl.DataFrame, required_features: set[str]) -> pl.DataFrame:
        return self.engine.enrich_for_features(df, required_features)

