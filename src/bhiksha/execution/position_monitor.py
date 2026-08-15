"""Open-position monitoring for strategy-managed exits."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision
from bhiksha.execution.thesis_exit import evaluate_underlying_thesis_exit
from bhiksha.execution.cartographer_invalidation import evaluate_invalidation
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
        positions: list[TrackedPosition] | None = None,
    ) -> list[ExitEvaluation]:
        evaluations: list[ExitEvaluation] = []
        active_positions = positions if positions is not None else self.position_tracker.active_positions()
        for position in active_positions:
            if position.symbol != symbol or position.quantity <= 0:
                continue
            if position.exit_mode is not None or position.exit_order_id is not None:
                continue
            deployment = deployments_by_id.get(position.deployment_id)
            if deployment is None:
                continue
            chart_decision = evaluate_invalidation(deployment, frame, position)
            if chart_decision is not None:
                evaluations.append(
                    ExitEvaluation(
                        deployment=deployment, position=position, decision=chart_decision
                    )
                )
                continue
            thesis_decision = evaluate_underlying_thesis_exit(deployment, frame, position)
            if thesis_decision is not None:
                if thesis_decision.exit:
                    evaluations.append(ExitEvaluation(deployment=deployment, position=position, decision=thesis_decision))
                    continue
            if not deployment.exit.use_algorithmic_exit:
                continue
            enriched = (enriched_frames or {}).get(deployment.deployment_id)
            if enriched is not None:
                decision = self.evaluator.evaluate_exit_on_enriched(deployment, enriched, position)
            else:
                decision = self.evaluator.evaluate_exit(deployment, frame, position)
            evaluations.append(ExitEvaluation(deployment=deployment, position=position, decision=decision))
        return evaluations
