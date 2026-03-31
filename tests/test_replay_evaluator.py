from datetime import datetime

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.config.loader import load_deployments
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.strategy.registry import default_strategy_registry


class CountingFeatureService(FeatureService):
    def __init__(self) -> None:
        super().__init__(engine=None)
        self.calls: list[tuple[str, ...]] = []

    def enrich_for_features(self, df: pl.DataFrame, required_features: set[str]) -> pl.DataFrame:
        self.calls.append(tuple(sorted(required_features)))
        enriched = df
        if "vma_10" not in enriched.columns:
            enriched = enriched.with_columns(pl.col("close").alias("vma_10"))
        if "impulse_regime_1h" not in enriched.columns:
            enriched = enriched.with_columns(pl.lit("bearish").alias("impulse_regime_1h"))
        return enriched


def test_replay_evaluator_reuses_enrichment_for_same_feature_set() -> None:
    deployments = load_deployments("config/deployments")
    qqq = next(d for d in deployments if d.deployment_id == "market_impulse_qqq_short_v1")
    sibling = qqq.model_copy(update={"deployment_id": "market_impulse_qqq_short_v2"})
    service = CountingFeatureService()
    evaluator = ReplaySignalEvaluator(service, default_strategy_registry())
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 29, 0),
                datetime(2026, 3, 30, 14, 30, 0),
                datetime(2026, 3, 30, 14, 31, 0),
            ],
            "open": [99.8, 99.9, 100.1],
            "high": [100.0, 100.1, 100.8],
            "low": [99.6, 99.8, 100.0],
            "close": [99.9, 100.0, 99.8],
            "volume": [1000.0, 1100.0, 1200.0],
        }
    )

    enriched = evaluator.prepare_enriched_frames(frame, [qqq, sibling])

    assert set(enriched) == {qqq.deployment_id, sibling.deployment_id}
    assert len(service.calls) == 1


def test_replay_evaluator_can_scan_historical_signals_on_enriched_frame() -> None:
    deployments = load_deployments("config/deployments")
    tsla = next(d for d in deployments if d.deployment_id == "jerk_pivot_momentum_tsla_short_v1")
    evaluator = ReplaySignalEvaluator(FeatureService(), default_strategy_registry())
    frame = pl.DataFrame(
        {
            "symbol": ["TSLA"] * 11,
            "timestamp": [datetime(2026, 3, 30, 15, 0, 0).replace(minute=minute) for minute in range(11)],
            "close": [100.05] * 10 + [99.90],
            "velocity_1m": [0.05] * 10 + [-0.20],
            "accel_1m": [0.02] * 10 + [-0.10],
            "jerk_1m": [1.0] * 10 + [-20.0],
            "vpoc_4h": [100.0] * 11,
            "volume": [1400.0] * 10 + [1500.0],
            "volume_ma_20": [1000.0] * 11,
        }
    )

    decisions = evaluator.scan_entry_history_on_enriched(tsla, frame)

    assert len(decisions) == 1
    assert decisions[0].signal is True
    assert decisions[0].direction.value == "short"
