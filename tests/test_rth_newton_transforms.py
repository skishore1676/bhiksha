from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from bhiksha.market_data.newton.engine import PhysicsEngine


def test_opening_vwap_rth_ignores_premarket_bars() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [
                datetime(2025, 1, 2, 14, 0, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 14, 31, tzinfo=timezone.utc),
                datetime(2025, 1, 3, 14, 30, tzinfo=timezone.utc),
            ],
            "open": [50.0, 100.0, 102.0, 200.0],
            "high": [50.5, 100.5, 102.5, 200.5],
            "low": [49.5, 99.5, 101.5, 199.5],
            "close": [50.0, 100.0, 102.0, 200.0],
            "volume": [1000.0, 100.0, 300.0, 100.0],
        }
    )

    result = PhysicsEngine().enrich_for_features(df, {"opening_vwap_rth"})

    values = result["opening_vwap_rth"].to_list()
    assert values[0] is None
    np.testing.assert_almost_equal(values[1], 100.0)
    np.testing.assert_almost_equal(values[2], 101.5)
    np.testing.assert_almost_equal(values[3], 200.0)


def test_prior_rth_close_atr_uses_regular_session_only() -> None:
    rows = [
        {
            "timestamp": datetime(2025, 1, 2, 14, 0, tzinfo=timezone.utc),
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.0,
            "volume": 1000.0,
        },
        {
            "timestamp": datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 1000.0,
        },
        {
            "timestamp": datetime(2025, 1, 3, 14, 0, tzinfo=timezone.utc),
            "open": 90.0,
            "high": 91.0,
            "low": 89.0,
            "close": 90.0,
            "volume": 1000.0,
        },
        {
            "timestamp": datetime(2025, 1, 3, 14, 30, tzinfo=timezone.utc),
            "open": 13.0,
            "high": 14.5,
            "low": 12.5,
            "close": 14.0,
            "volume": 1000.0,
        },
    ]
    df = pl.DataFrame(rows)

    result = PhysicsEngine().enrich_for_features(
        df,
        {
            "prior_rth_close",
            "daily_rth_atr_14",
            "atr_distance_from_prior_rth_close",
            "gap_rth_atr",
            "gap_state_rth_open",
        },
    )

    premarket_day2 = result.row(2, named=True)
    rth_day2 = result.row(3, named=True)
    assert premarket_day2["gap_state_rth_open"] is None
    np.testing.assert_almost_equal(rth_day2["prior_rth_close"], 11.0)
    np.testing.assert_almost_equal(rth_day2["daily_rth_atr_14"], 3.0)
    np.testing.assert_almost_equal(rth_day2["gap_rth_atr"], 2.0 / 3.0)
    assert rth_day2["gap_state_rth_open"] == "gap_up_small"
    np.testing.assert_almost_equal(rth_day2["atr_distance_from_prior_rth_close"], 1.0)


def test_relative_volume_rth_transform_ignores_premarket_bars() -> None:
    df = pl.DataFrame(
        {
            "timestamp": [
                datetime(2025, 1, 2, 14, 0, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 14, 31, tzinfo=timezone.utc),
                datetime(2025, 1, 2, 14, 32, tzinfo=timezone.utc),
            ],
            "open": [1.0] * 4,
            "high": [1.0] * 4,
            "low": [1.0] * 4,
            "close": [1.0] * 4,
            "volume": [5000.0, 100.0, 200.0, 300.0],
        }
    )

    result = PhysicsEngine().enrich_for_features(df, {"relative_volume_rth_2"})

    values = result["relative_volume_rth_2"].to_list()
    assert values[0] is None
    assert values[1] is None
    np.testing.assert_almost_equal(values[2], 200.0 / 150.0)
    np.testing.assert_almost_equal(values[3], 300.0 / 250.0)
