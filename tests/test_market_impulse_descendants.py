"""Unit tests for the Market Impulse descendant entry modes.

Semantics mirror mala_v2 ``src/strategy/market_impulse.py``; parameter values
come from the blocked Mala_Evidence_v1 rows (see
tests/fixtures/mala_evidence/capability_family_rows.json).
"""

from datetime import datetime, timedelta

import polars as pl
import pytest

from bhiksha.strategy.market_impulse import MarketImpulseStrategy


# 2026-07-06 is EDT (UTC-4): 13:35 UTC == 09:35 ET.
_SESSION_OPEN_UTC = datetime(2026, 7, 6, 13, 30, 0)

SHALLOW_SPRING_SHORT_PARAMS = {
    # mi-desc-shallow-spring-semiconductors-m1__amd_short
    "direction": "short",
    "entry_mode": "same_bar_shallow_reclaim",
    "entry_buffer_minutes": 3,
    "entry_window_minutes": 90,
    "max_vma_excursion_pct": 0.0005,
    "min_reclaim_margin_pct": 0.0002,
    "regime_timeframe": "5m",
    "use_volume_filter": False,
    "vwma_periods": [8, 21, 34],
}

HIGH_CLOSE_SHORT_PARAMS = {
    # mi-desc-high-close-semiconductors-m1__amd_short
    "direction": "short",
    "entry_mode": "close_location_reclaim",
    "entry_buffer_minutes": 3,
    "entry_window_minutes": 90,
    "max_vma_excursion_pct": 0.002,
    "min_close_location": 0.6,
    "min_reclaim_margin_pct": 0.0,
    "regime_timeframe": "5m",
    "use_volume_filter": False,
    "vwma_periods": [8, 21, 34],
}

PUSH_THROUGH_SHORT_PARAMS = {
    # mi-desc-push-through-semiconductors-m1__smh_short
    "direction": "short",
    "entry_mode": "continuation_confirmation",
    "confirmation_margin_pct": 0.0003,
    "confirmation_type": "break_reclaim_high_low",
    "confirmation_window_bars": 1,
    "entry_buffer_minutes": 3,
    "entry_window_minutes": 45,
    "max_vma_excursion_pct": 0.002,
    "min_reclaim_margin_pct": 0.0002,
    "regime_timeframe": "5m",
    "use_volume_filter": False,
    "vwma_periods": [8, 21, 34],
}

PUSH_THROUGH_LONG_PARAMS = {
    # mi-desc-push-through-semiconductors-m1__mu_long
    "direction": "long",
    "entry_mode": "continuation_confirmation",
    "confirmation_margin_pct": 0.0,
    "confirmation_type": "break_reclaim_high_low",
    "confirmation_window_bars": 2,
    "entry_buffer_minutes": 3,
    "entry_window_minutes": 60,
    "max_vma_excursion_pct": 0.0005,
    "min_reclaim_margin_pct": 0.0,
    "regime_timeframe": "5m",
    "use_volume_filter": False,
    "vwma_periods": [8, 21, 34],
}


def _frame(bars: list[dict], symbol: str = "AMD") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(bars),
            "timestamp": [bar["timestamp"] for bar in bars],
            "high": [bar["high"] for bar in bars],
            "low": [bar["low"] for bar in bars],
            "close": [bar["close"] for bar in bars],
            "vma_10": [bar.get("vma", 100.0) for bar in bars],
            "impulse_regime_5m": [bar.get("regime", "bearish") for bar in bars],
        }
    )


def _bar(minutes_after_open: int, *, high: float, low: float, close: float, vma: float = 100.0, regime: str = "bearish") -> dict:
    return {
        "timestamp": _SESSION_OPEN_UTC + timedelta(minutes=minutes_after_open),
        "high": high,
        "low": low,
        "close": close,
        "vma": vma,
        "regime": regime,
    }


# ── same_bar_shallow_reclaim (MI Shallow Spring) ────────────────────────────


def test_shallow_spring_short_fires_on_shallow_pierce_and_reclaim() -> None:
    strategy = MarketImpulseStrategy()
    # Excursion (100.04 - 100) / 100 = 0.0004 <= 0.0005; close < 100 * (1 - 0.0002).
    frame = _frame([_bar(5, high=100.04, low=99.90, close=99.95)])

    decision = strategy.evaluate_entry(frame, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert "same_bar_shallow_reclaim_short" in decision.reason


def test_shallow_spring_short_blocked_when_excursion_too_deep() -> None:
    strategy = MarketImpulseStrategy()
    # Excursion (100.10 - 100) / 100 = 0.001 > 0.0005.
    frame = _frame([_bar(5, high=100.10, low=99.90, close=99.95)])

    decision = strategy.evaluate_entry(frame, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS)

    assert decision.signal is False


def test_shallow_spring_short_reclaim_margin_is_strict_boundary() -> None:
    strategy = MarketImpulseStrategy()
    # close == vma * (1 - 0.0002) exactly: strict inequality must block.
    at_margin = _frame([_bar(5, high=100.04, low=99.90, close=99.98)])
    below_margin = _frame([_bar(5, high=100.04, low=99.90, close=99.9799)])

    assert strategy.evaluate_entry(at_margin, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS).signal is False
    assert strategy.evaluate_entry(below_margin, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS).signal is True


def test_shallow_spring_short_blocked_outside_entry_window() -> None:
    strategy = MarketImpulseStrategy()
    # 95 minutes after the open is past the 90 minute window (11:00 ET).
    frame = _frame([_bar(95, high=100.04, low=99.90, close=99.95)])

    decision = strategy.evaluate_entry(frame, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS)

    assert decision.signal is False
    assert "time_window_blocked" in decision.reason


def test_shallow_spring_short_blocked_when_regime_not_bearish() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame([_bar(5, high=100.04, low=99.90, close=99.95, regime="neutral")])

    decision = strategy.evaluate_entry(frame, "shallow_spring_test", SHALLOW_SPRING_SHORT_PARAMS)

    assert decision.signal is False
    assert "regime_neutral" in decision.reason


# ── close_location_reclaim (MI High Close Reclaim) ──────────────────────────


def test_high_close_reclaim_short_fires_when_close_in_bottom_of_range() -> None:
    strategy = MarketImpulseStrategy()
    # close_location = (99.70 - 99.50) / 0.60 = 0.333 <= 1 - 0.6.
    frame = _frame([_bar(5, high=100.10, low=99.50, close=99.70)])

    decision = strategy.evaluate_entry(frame, "high_close_test", HIGH_CLOSE_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert "close_location_reclaim_short" in decision.reason
    assert decision.features["close_location"] == pytest.approx(0.2 / 0.6)


def test_high_close_reclaim_short_blocked_when_close_location_too_high() -> None:
    strategy = MarketImpulseStrategy()
    # close_location = (99.85 - 99.50) / 0.60 = 0.583 > 0.4: reclaim closed too
    # high inside the bar for a short.
    frame = _frame([_bar(5, high=100.10, low=99.50, close=99.85)])

    decision = strategy.evaluate_entry(frame, "high_close_test", HIGH_CLOSE_SHORT_PARAMS)

    assert decision.signal is False


def test_high_close_reclaim_long_requires_close_near_bar_high() -> None:
    strategy = MarketImpulseStrategy()
    params = dict(HIGH_CLOSE_SHORT_PARAMS, direction="long")
    # close_location = (100.40 - 99.90) / 0.60 = 0.833 >= 0.6.
    fires = _frame([_bar(5, high=100.50, low=99.90, close=100.40, regime="bullish")])
    # close_location = (100.10 - 99.90) / 0.60 = 0.333 < 0.6.
    blocked = _frame([_bar(5, high=100.50, low=99.90, close=100.10, regime="bullish")])

    assert strategy.evaluate_entry(fires, "high_close_test", params).signal is True
    assert strategy.evaluate_entry(fires, "high_close_test", params).direction.value == "long"
    assert strategy.evaluate_entry(blocked, "high_close_test", params).signal is False


def test_high_close_reclaim_short_blocked_by_direction_filter() -> None:
    strategy = MarketImpulseStrategy()
    params = dict(HIGH_CLOSE_SHORT_PARAMS, direction="long")
    frame = _frame([_bar(5, high=100.10, low=99.50, close=99.70)])

    decision = strategy.evaluate_entry(frame, "high_close_test", params)

    assert decision.signal is False


# ── continuation_confirmation (MI Push Through) ─────────────────────────────


def test_push_through_short_fires_when_next_bar_breaks_reclaim_low() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame(
        [
            # Reclaim bar arms the short: pierce above VMA, close back below with margin.
            _bar(5, high=100.10, low=99.70, close=99.90),
            # Confirmation bar breaks the reclaim bar low (99.65 < 99.70).
            _bar(6, high=99.95, low=99.65, close=99.80),
        ]
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"
    assert "continuation_confirmation_short" in decision.reason
    assert decision.features["reclaim_bar_low"] == pytest.approx(99.70)
    assert decision.features["confirmation_age_bars"] == 1


def test_push_through_arming_bar_does_not_signal_itself() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame([_bar(5, high=100.10, low=99.70, close=99.90)])

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is False
    assert "awaiting_continuation_confirmation" in decision.reason


def test_push_through_short_blocked_when_break_arrives_after_window() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            # Age 1: inside the window but no break of the reclaim low.
            _bar(6, high=99.95, low=99.75, close=99.85),
            # Age 2 > confirmation_window_bars=1: pending cleared, break ignored.
            _bar(7, high=99.90, low=99.60, close=99.75),
        ]
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is False


def test_push_through_short_blocked_when_regime_flips_before_confirmation() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            # Regime flip clears the pending short even though the low breaks.
            _bar(6, high=99.95, low=99.60, close=99.75, regime="bullish"),
        ]
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is False


def test_push_through_arm_before_window_confirm_inside_window_fires() -> None:
    strategy = MarketImpulseStrategy()
    # Research semantics: only the confirmation bar needs the time window.
    # The window opens at 09:33 ET; the reclaim bar prints at 09:32 ET.
    frame = _frame(
        [
            _bar(2, high=100.10, low=99.70, close=99.90),
            _bar(3, high=99.95, low=99.65, close=99.80),
        ]
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "short"


def test_push_through_confirm_bar_outside_window_is_blocked() -> None:
    strategy = MarketImpulseStrategy()
    # Window is 09:33-10:15 ET (45 minutes). Arm at 10:15, confirm try at 10:16.
    frame = _frame(
        [
            _bar(45, high=100.10, low=99.70, close=99.90),
            _bar(46, high=99.95, low=99.65, close=99.80),
        ]
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_SHORT_PARAMS)

    assert decision.signal is False
    assert "time_window_blocked" in decision.reason


def test_push_through_long_confirms_within_two_bar_window() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame(
        [
            # Reclaim bar: shallow dip below VMA (excursion 0.0004 <= 0.0005),
            # close back above. Arms the long with high=100.20.
            _bar(5, high=100.20, low=99.96, close=100.05, regime="bullish"),
            # Age 1: no break of the reclaim high yet; holds above the VMA so
            # it does not re-arm a fresh reclaim state.
            _bar(6, high=100.10, low=100.01, close=100.05, regime="bullish"),
            # Age 2 == confirmation_window_bars: break of 100.20 confirms.
            _bar(7, high=100.30, low=100.00, close=100.25, regime="bullish"),
        ],
        symbol="MU",
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_LONG_PARAMS)

    assert decision.signal is True
    assert decision.direction.value == "long"
    assert decision.features["reclaim_bar_high"] == pytest.approx(100.20)


def test_push_through_re_arms_on_newest_reclaim_bar() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame(
        [
            # First reclaim bar (high 100.20).
            _bar(5, high=100.20, low=99.96, close=100.05, regime="bullish"),
            # Second reclaim bar re-arms the pending long (high 100.10).
            _bar(6, high=100.10, low=99.97, close=100.03, regime="bullish"),
            # Breaks the NEWEST reclaim high (100.10), not the first one.
            _bar(7, high=100.15, low=100.00, close=100.12, regime="bullish"),
        ],
        symbol="MU",
    )

    decision = strategy.evaluate_entry(frame, "push_through_test", PUSH_THROUGH_LONG_PARAMS)

    assert decision.signal is True
    assert decision.features["reclaim_bar_high"] == pytest.approx(100.10)
    assert decision.features["confirmation_age_bars"] == 1


def test_push_through_close_beyond_reclaim_confirmation_type() -> None:
    strategy = MarketImpulseStrategy()
    params = dict(PUSH_THROUGH_SHORT_PARAMS, confirmation_type="close_beyond_reclaim")
    # Wick below the reclaim low is no longer enough: close must break it.
    wick_only = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            _bar(6, high=99.95, low=99.65, close=99.75),
        ]
    )
    close_break = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            _bar(6, high=99.95, low=99.60, close=99.65),
        ]
    )

    assert strategy.evaluate_entry(wick_only, "push_through_test", params).signal is False
    assert strategy.evaluate_entry(close_break, "push_through_test", params).signal is True


def test_push_through_vma_margin_confirmation_type() -> None:
    strategy = MarketImpulseStrategy()
    params = dict(PUSH_THROUGH_SHORT_PARAMS, confirmation_type="vma_margin", confirmation_margin_pct=0.001)
    # Short vma_margin confirm: close <= vma * (1 - 0.001) = 99.90.
    confirmed = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            _bar(6, high=99.95, low=99.75, close=99.88),
        ]
    )
    not_confirmed = _frame(
        [
            _bar(5, high=100.10, low=99.70, close=99.90),
            _bar(6, high=99.98, low=99.91, close=99.95),
        ]
    )

    assert strategy.evaluate_entry(confirmed, "push_through_test", params).signal is True
    assert strategy.evaluate_entry(not_confirmed, "push_through_test", params).signal is False


# ── guardrails ───────────────────────────────────────────────────────────────


def test_descendant_volume_filter_is_rejected_loudly() -> None:
    strategy = MarketImpulseStrategy()
    params = dict(SHALLOW_SPRING_SHORT_PARAMS, use_volume_filter=True)
    frame = _frame([_bar(5, high=100.04, low=99.90, close=99.95)])

    with pytest.raises(ValueError, match="volume filter is not runtime-supported"):
        strategy.evaluate_entry(frame, "shallow_spring_test", params)

    assert "relative_volume_20" in strategy.required_features(params)


def test_delayed_reclaim_entry_mode_is_rejected() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame([_bar(5, high=100.04, low=99.90, close=99.95)])

    with pytest.raises(ValueError, match="research-only"):
        strategy.evaluate_entry(frame, "delayed_reclaim_test", {"entry_mode": "delayed_reclaim"})


def test_unknown_entry_mode_is_rejected() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame([_bar(5, high=100.04, low=99.90, close=99.95)])

    with pytest.raises(ValueError, match="Unsupported Market Impulse entry_mode"):
        strategy.evaluate_entry(frame, "unknown_mode_test", {"entry_mode": "made_up_mode"})


def test_baseline_cross_reclaim_behavior_unchanged_with_explicit_entry_mode() -> None:
    strategy = MarketImpulseStrategy()
    frame = _frame([_bar(5, high=100.10, low=99.90, close=99.80)])
    params = {
        "direction": "short",
        "entry_buffer_minutes": 3,
        "entry_window_minutes": 90,
        "regime_timeframe": "5m",
    }

    implicit = strategy.evaluate_entry(frame, "baseline_test", params)
    explicit = strategy.evaluate_entry(frame, "baseline_test", dict(params, entry_mode="cross_reclaim"))

    assert implicit.signal is True
    assert explicit.signal is True
    assert implicit.direction == explicit.direction
    assert implicit.reason == explicit.reason


def test_descendant_required_features_match_baseline_columns() -> None:
    strategy = MarketImpulseStrategy()

    features = strategy.required_features(HIGH_CLOSE_SHORT_PARAMS)

    assert "market_impulse:5m:vma_10:vwma_8_21_34" in features
    assert not any(feature.startswith("relative_volume") for feature in features)
