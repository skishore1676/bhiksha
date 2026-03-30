"""Replay-first signal evaluation helpers."""

from __future__ import annotations

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.registry import StrategyRegistry


class ReplaySignalEvaluator:
    """Evaluate deployments against a historical or synthetic frame."""

    def __init__(self, feature_service: FeatureService, strategy_registry: StrategyRegistry) -> None:
        self.feature_service = feature_service
        self.strategy_registry = strategy_registry

    def _enrich(self, deployment: DeploymentManifest, frame: pl.DataFrame) -> pl.DataFrame:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        working = frame
        if "symbol" not in working.columns:
            working = working.with_columns(pl.lit(deployment.symbol).alias("symbol"))
        return self.feature_service.enrich_for_features(
            working,
            strategy.required_features(deployment.strategy.params),
        )

    def evaluate_entry(self, deployment: DeploymentManifest, frame: pl.DataFrame) -> SignalDecision:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        enriched = self._enrich(deployment, frame)
        return strategy.evaluate_entry(enriched, deployment.deployment_id, deployment.strategy.params)

    def evaluate_exit(
        self,
        deployment: DeploymentManifest,
        frame: pl.DataFrame,
        position: TrackedPosition,
    ) -> ExitDecision:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        enriched = self._enrich(deployment, frame)
        return strategy.evaluate_exit(enriched, deployment.deployment_id, deployment.strategy.params, position)
