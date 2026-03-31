"""Replay-first signal evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import polars as pl

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitDecision, SignalDecision
from bhiksha.market_data.session import as_et_time
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.state.position_tracker import TrackedPosition
from bhiksha.strategy.registry import StrategyRegistry
from bhiksha.strategy.base import coerce_time


@dataclass(slots=True, frozen=True)
class ReplayTrade:
    entry_index: int
    entry_decision: SignalDecision
    exit_index: int | None = None
    exit_decision: ExitDecision | None = None
    exit_category: str = "open"
    premium_exit_status: str = "not_available"


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
        return [
            decision
            for _, decision in self.scan_entry_history_with_index_on_enriched(
                deployment,
                enriched,
                start_at=start_at,
                signals_only=signals_only,
            )
        ]

    def scan_entry_history_with_index_on_enriched(
        self,
        deployment: DeploymentManifest,
        enriched: pl.DataFrame,
        *,
        start_at: int = 0,
        signals_only: bool = True,
    ) -> list[tuple[int, SignalDecision]]:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        decisions: list[tuple[int, SignalDecision]] = []
        for index in range(max(start_at, 0), enriched.height):
            decision = strategy.evaluate_entry(
                enriched.head(index + 1),
                deployment.deployment_id,
                deployment.strategy.params,
            )
            if signals_only and not decision.signal:
                continue
            decisions.append((index, decision))
        return decisions

    def evaluate_exit(
        self,
        deployment: DeploymentManifest,
        frame: pl.DataFrame,
        position: TrackedPosition,
    ) -> ExitDecision:
        enriched = self.prepare_enriched_frames(frame, [deployment])[deployment.deployment_id]
        return self.evaluate_exit_on_enriched(deployment, enriched, position)

    def scan_trade_history_on_enriched(
        self,
        deployment: DeploymentManifest,
        enriched: pl.DataFrame,
        *,
        start_at: int = 0,
    ) -> list[ReplayTrade]:
        strategy = self.strategy_registry.get(deployment.strategy.key)
        trades: list[ReplayTrade] = []
        active_trade: ReplayTrade | None = None
        active_position: TrackedPosition | None = None
        hard_flat_time = _deployment_hard_flat_time(deployment)

        for index in range(max(start_at, 0), enriched.height):
            frame_slice = enriched.head(index + 1)
            latest = frame_slice.tail(1).to_dicts()[0]

            if active_trade is None:
                entry = strategy.evaluate_entry(
                    frame_slice,
                    deployment.deployment_id,
                    deployment.strategy.params,
                )
                if not entry.signal:
                    continue
                active_trade = ReplayTrade(entry_index=index, entry_decision=entry)
                active_position = TrackedPosition(
                    symbol=deployment.symbol,
                    deployment_id=deployment.deployment_id,
                    option_symbol="REPLAY_OPTION_PLACEHOLDER",
                )
                continue

            time_exit = _hard_flat_exit_decision(deployment, latest, hard_flat_time)
            if time_exit is not None:
                trades.append(
                    ReplayTrade(
                        entry_index=active_trade.entry_index,
                        entry_decision=active_trade.entry_decision,
                        exit_index=index,
                        exit_decision=time_exit,
                        exit_category="time_exit",
                    )
                )
                active_trade = None
                active_position = None
                continue

            if deployment.exit.use_algorithmic_exit and active_position is not None:
                decision = strategy.evaluate_exit(
                    frame_slice,
                    deployment.deployment_id,
                    deployment.strategy.params,
                    active_position,
                )
                if decision.exit:
                    trades.append(
                        ReplayTrade(
                            entry_index=active_trade.entry_index,
                            entry_decision=active_trade.entry_decision,
                            exit_index=index,
                            exit_decision=decision,
                            exit_category="strategy_exit",
                        )
                    )
                    active_trade = None
                    active_position = None

        if active_trade is not None:
            trades.append(active_trade)

        return trades


def _deployment_hard_flat_time(deployment: DeploymentManifest) -> time:
    return coerce_time(deployment.exit.hard_flat_time_et or deployment.risk.hard_flat_time_et or "15:55")


def _hard_flat_exit_decision(
    deployment: DeploymentManifest,
    latest: dict[str, object],
    hard_flat_time: time,
) -> ExitDecision | None:
    timestamp = latest["timestamp"]
    if as_et_time(timestamp) < hard_flat_time:
        return None
    return ExitDecision(
        deployment_id=deployment.deployment_id,
        symbol=str(latest["symbol"]),
        timestamp=timestamp,
        exit=True,
        action="square_off",
        reason=["hard_flat_time_reached"],
        cancel_protection_orders=True,
    )
