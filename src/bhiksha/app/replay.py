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

    def _normalize_frame(self, deployment: DeploymentManifest, frame: pl.DataFrame) -> pl.DataFrame:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        working = frame
        if "symbol" not in working.columns:
            working = working.with_columns(pl.lit(deployment.symbol).alias("symbol"))
        return working

    def _required_features(self, deployment: DeploymentManifest) -> set[str]:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        return strategy.required_features(deployment.strategy.params)

    def prepare_enriched_frames(
        self,
        frame: pl.DataFrame,
        deployments: list[DeploymentManifest],
    ) -> dict[str, pl.DataFrame]:
        if not deployments:
            return {}
        normalized = self._normalize_frame(deployments[0], frame)
        grouped: dict[tuple[str, tuple[str, ...]], list[DeploymentManifest]] = {}
        for deployment in deployments:
            required = tuple(sorted(self._required_features(deployment)))
            grouped.setdefault((deployment.strategy.key, required), []).append(deployment)

        prepared: dict[str, pl.DataFrame] = {}
        for (_, required), grouped_deployments in grouped.items():
            enriched = self.feature_service.enrich_for_features(normalized, set(required))
            for deployment in grouped_deployments:
                prepared[deployment.deployment_id] = enriched
        return prepared

    def evaluate_entry_on_enriched(self, deployment: DeploymentManifest, enriched: pl.DataFrame) -> SignalDecision:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        return strategy.evaluate_entry(enriched, deployment.deployment_id, deployment.strategy.params)

    def evaluate_exit_on_enriched(
        self,
        deployment: DeploymentManifest,
        enriched: pl.DataFrame,
        position: TrackedPosition,
    ) -> ExitDecision:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        return strategy.evaluate_exit(enriched, deployment.deployment_id, deployment.strategy.params, position)

    def evaluate_entry(self, deployment: DeploymentManifest, frame: pl.DataFrame) -> SignalDecision:
        enriched = self.prepare_enriched_frames(frame, [deployment])[deployment.deployment_id]
        return self.evaluate_entry_on_enriched(deployment, enriched)

    def scan_entry_history_on_enriched(
        self,
        deployment: DeploymentManifest,
        enriched: pl.DataFrame,
        *,
        start_at: int = 0,
        signals_only: bool = True,
    ) -> list[SignalDecision]:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        decisions: list[SignalDecision] = []
        for index in range(max(start_at, 0), enriched.height):
            decision = strategy.evaluate_entry(
                enriched.head(index + 1),
                deployment.deployment_id,
                deployment.strategy.params,
            )
            if signals_only and not decision.signal:
                continue
            decisions.append(decision)
        return decisions

    def evaluate_exit(
        self,
        deployment: DeploymentManifest,
        frame: pl.DataFrame,
        position: TrackedPosition,
    ) -> ExitDecision:
        enriched = self.prepare_enriched_frames(frame, [deployment])[deployment.deployment_id]
        return self.evaluate_exit_on_enriched(deployment, enriched, position)
