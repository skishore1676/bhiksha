from datetime import datetime

import polars as pl

from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.strategy.registry import default_strategy_registry
from historical_config import load_historical_deployments


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
    deployments = load_historical_deployments()
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
    deployments = load_historical_deployments()
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


def test_replay_evaluator_can_return_historical_signal_indexes() -> None:
    deployments = load_historical_deployments()
    tsla = next(d for d in deployments if d.deployment_id == "jerk_pivot_momentum_tsla_short_v1")
    evaluator = ReplaySignalEvaluator(FeatureService(), default_strategy_registry())
    frame = pl.DataFrame(
        {
            "symbol": ["TSLA"] * 11,
            "timestamp": [datetime(2026, 3, 30, 15, 0, 0).replace(minute=minute) for minute in range(11)],
            "open": [100.1] * 10 + [99.95],
            "close": [100.05] * 10 + [99.90],
            "velocity_1m": [0.05] * 10 + [-0.20],
            "accel_1m": [0.02] * 10 + [-0.10],
            "jerk_1m": [1.0] * 10 + [-20.0],
            "vpoc_4h": [100.0] * 11,
            "volume": [1400.0] * 10 + [1500.0],
            "volume_ma_20": [1000.0] * 11,
        }
    )

    indexed_decisions = evaluator.scan_entry_history_with_index_on_enriched(tsla, frame)

    assert len(indexed_decisions) == 1
    assert indexed_decisions[0][0] == 10
    assert indexed_decisions[0][1].signal is True


def test_replay_evaluator_can_pair_entry_with_strategy_exit() -> None:
    deployments = load_historical_deployments()
    qqq = next(d for d in deployments if d.deployment_id == "market_impulse_qqq_short_v1")
    evaluator = ReplaySignalEvaluator(FeatureService(), default_strategy_registry())
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 29, 0),
                datetime(2026, 3, 30, 14, 30, 0),
                datetime(2026, 3, 30, 14, 31, 0),
            ],
            "open": [100.1, 100.2, 100.3],
            "high": [100.2, 100.4, 100.7],
            "low": [99.8, 99.9, 100.2],
            "close": [100.2, 99.9, 100.6],
            "volume": [1000.0, 1100.0, 1200.0],
            "vma_10": [100.0, 100.0, 100.0],
            "impulse_regime_1h": ["bearish", "bearish", "bearish"],
        }
    )

    trades = evaluator.scan_trade_history_on_enriched(qqq, frame)

    assert len(trades) == 1
    assert trades[0].entry_index == 1
    assert trades[0].exit_index == 2
    assert trades[0].exit_category == "strategy_exit"
    assert trades[0].exit_decision is not None
    assert trades[0].exit_decision.reason == ["vma_reclaim_exit"]


def test_replay_evaluator_can_pair_entry_with_time_exit() -> None:
    deployments = load_historical_deployments()
    qqq = next(d for d in deployments if d.deployment_id == "market_impulse_qqq_short_v1")
    qqq = qqq.model_copy(update={"exit": qqq.exit.model_copy(update={"use_algorithmic_exit": False})})
    evaluator = ReplaySignalEvaluator(FeatureService(), default_strategy_registry())
    frame = pl.DataFrame(
        {
            "symbol": ["QQQ", "QQQ", "QQQ"],
            "timestamp": [
                datetime(2026, 3, 30, 14, 29, 0),
                datetime(2026, 3, 30, 14, 30, 0),
                datetime(2026, 3, 30, 19, 55, 0),
            ],
            "open": [100.1, 100.2, 99.8],
            "high": [100.2, 100.4, 100.0],
            "low": [99.8, 99.9, 99.5],
            "close": [100.2, 99.9, 99.7],
            "volume": [1000.0, 1100.0, 900.0],
            "vma_10": [100.0, 100.0, 100.0],
            "impulse_regime_1h": ["bearish", "bearish", "bearish"],
        }
    )

    trades = evaluator.scan_trade_history_on_enriched(qqq, frame)

    assert len(trades) == 1
    assert trades[0].entry_index == 1
    assert trades[0].exit_index == 2
    assert trades[0].exit_category == "time_exit"
    assert trades[0].exit_decision is not None
    assert trades[0].exit_decision.reason == ["hard_flat_time_reached"]
