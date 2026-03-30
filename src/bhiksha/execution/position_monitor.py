"""Open-position monitoring for strategy-managed exits."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision
from bhiksha.state.position_tracker import PositionTracker, TrackedPosition


@dataclass(slots=True, frozen=True)
class ExitEvaluation:
    deployment: DeploymentManifest
    position: TrackedPosition
    decision: ExitDecision


class PositionMonitor:
    """Evaluate open positions on completed bars using strategy exit logic."""

    def __init__(self, evaluator: ReplaySignalEvaluator, position_tracker: PositionTracker) -> None:
        self.evaluator = evaluator
        self.position_tracker = position_tracker

    def evaluate_symbol(
        self,
        symbol: str,
        frame: pl.DataFrame,
        deployments_by_id: dict[str, DeploymentManifest],
        *,
        enriched_frames: dict[str, pl.DataFrame] | None = None,
    ) -> list[ExitEvaluation]:
        evaluations: list[ExitEvaluation] = []
        for position in self.position_tracker.active_positions():
            if position.symbol != symbol or position.quantity <= 0:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None or not deployment.exit.use_algorithmic_exit:
                continue
            enriched = (enriched_frames or {}).get(deployment.deployment_id)
            if enriched is not None:
                decision = self.evaluator.evaluate_exit_on_enriched(deployment, enriched, position)
            else:
                decision = self.evaluator.evaluate_exit(deployment, frame, position)
            evaluations.append(ExitEvaluation(deployment=deployment, position=position, decision=decision))
        return evaluations
