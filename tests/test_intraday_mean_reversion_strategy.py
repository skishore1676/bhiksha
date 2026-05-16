from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from bhiksha.domain.enums import SignalDirection
from bhiksha.strategy.intraday_mean_reversion import IntradayMeanReversionStrategy
from bhiksha.strategy.registry import default_strategy_registry


def test_intraday_mean_reversion_short_signal_from_prior_rth_stretch() -> None:
    strategy = IntradayMeanReversionStrategy()
    frame = _frame_for_short_reversal()
    params = {
        "stretch_source": "prior_rth_close_atr",
        "stretch_threshold": 2.0,
        "reversal_range_minutes": 5,
        "confirming_bars": 1,
        "velocity_filter": "no_filter",
        "stage_filter": "no_filter",
        "gap_state_filter": "no_filter",
        "use_jerk_confirmation": False,
        "allow_long": True,
        "allow_short": True,
    }

    decision = strategy.evaluate_entry(frame, "iwm-reversion", params)
    signals = strategy.generate_signals(frame, params)

    assert decision.signal is True
    assert decision.direction == SignalDirection.SHORT
    assert signals["signal"].to_list()[-1] is True
    assert signals["signal_direction"].to_list()[-1] == "short"


def test_intraday_mean_reversion_registered_in_default_registry() -> None:
    registry = default_strategy_registry()

    assert registry.get("intraday_mean_reversion_extremes").key == (
        "intraday_mean_reversion_extremes"
    )


def _frame_for_short_reversal() -> pl.DataFrame:
    start = datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc)
    closes = [103.0, 104.0, 105.0, 104.0, 103.0, 102.5]
    lows = [102.9, 103.9, 104.9, 103.9, 102.9, 102.4]
    highs = [103.2, 104.2, 105.2, 104.2, 103.2, 103.0]
    return pl.DataFrame(
        {
            "symbol": ["IWM"] * len(closes),
            "timestamp": [start + timedelta(minutes=idx) for idx in range(len(closes))],
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * len(closes),
            "opening_vwap_rth": [101.0] * len(closes),
            "prior_rth_close": [100.0] * len(closes),
            "daily_rth_atr_14": [1.0] * len(closes),
            "atr_distance_from_prior_rth_close": [
                (close - 100.0) / 1.0 for close in closes
            ],
            "gap_state_rth_open": ["flat"] * len(closes),
            "velocity_5": [1.0] * len(closes),
            "market_pulse_stage": ["bullish"] * len(closes),
        }
    )
