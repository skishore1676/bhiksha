"""Market Impulse strategy plugin for the live runtime.

Implements the baseline cross-and-reclaim entry plus the researched descendant
entry modes (Mala ``src/strategy/market_impulse.py`` is the semantic source of
truth):

- ``cross_reclaim``             — baseline: bar pierces the VMA and closes back
                                  on the regime side within the entry window.
- ``same_bar_shallow_reclaim``  — "MI Shallow Spring": baseline plus a shallow
                                  VMA-excursion cap and a reclaim-close margin.
- ``close_location_reclaim``    — "MI High Close Reclaim": baseline plus the
                                  excursion cap, reclaim margin, and a
                                  close-location gate inside the bar range.
- ``continuation_confirmation`` — "MI Push Through": a reclaim bar arms a
                                  pending setup; a later bar inside the
                                  confirmation window must push through the
                                  reclaim bar's extreme (or clear the
                                  configured margin) to fire.

``delayed_reclaim`` ("MI Second Touch") remains research-only and is rejected
here; the capability manifest keeps it ``unsupported``.
"""

from __future__ import annotations

from datetime import time
from typing import Any

import polars as pl

from bhiksha.domain.models import ExitDecision
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision
from bhiksha.market_data.session import as_et_time
from bhiksha.state.position_tracker import TrackedPosition


BASELINE_ENTRY_MODE = "cross_reclaim"
IMPLEMENTED_DESCENDANT_ENTRY_MODES = (
    "same_bar_shallow_reclaim",
    "close_location_reclaim",
    "continuation_confirmation",
)
RESEARCH_ONLY_ENTRY_MODES = ("delayed_reclaim",)
CONFIRMATION_TYPES = ("break_reclaim_high_low", "close_beyond_reclaim", "vma_margin")

_ENTRY_MODE_ALIASES = {
    "market impulse (cross & reclaim)": BASELINE_ENTRY_MODE,
    "mi shallow spring": "same_bar_shallow_reclaim",
    "mi second touch": "delayed_reclaim",
    "mi high close reclaim": "close_location_reclaim",
    "mi push through": "continuation_confirmation",
}


class MarketImpulseStrategy:
    """Evaluate the latest bar for the Market Impulse setup family."""

    key = "market_impulse"

    def required_features(self, params: dict[str, Any]) -> set[str]:
        vma_length = int(params.get("vma_length", 10))
        regime_timeframe = str(params.get("regime_timeframe", "1h"))
        vwma_periods = _coerce_vwma_periods(params.get("vwma_periods"))
        if vwma_periods is not None:
            vwma_spec = "_".join(str(period) for period in vwma_periods)
            impulse_request = f"market_impulse:{regime_timeframe}:vma_{vma_length}:vwma_{vwma_spec}"
        else:
            impulse_request = f"market_impulse:{regime_timeframe}:vma_{vma_length}"
        required = {
            "timestamp",
            "symbol",
            "close",
            "high",
            "low",
            impulse_request,
        }
        if _coerce_bool(params.get("use_volume_filter")):
            # Research gates descendants on relative volume; the runtime has no
            # matching relative_volume_<period> feature yet. Requesting the raw
            # research column keeps feature-contract probes failing loudly
            # instead of silently dropping the filter.
            relative_volume_period = int(params.get("relative_volume_period", 20))
            required.add(f"relative_volume_{relative_volume_period}")
        return required

    def evaluate_entry(self, frame: pl.DataFrame, deployment_id: str, params: dict[str, Any]) -> SignalDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Market Impulse on an empty frame")

        entry_mode = _normalize_entry_mode(params.get("entry_mode"))
        if entry_mode == BASELINE_ENTRY_MODE:
            return self._evaluate_cross_reclaim_entry(frame, deployment_id, params)
        if entry_mode in RESEARCH_ONLY_ENTRY_MODES:
            raise ValueError(
                f"Market Impulse entry_mode {entry_mode!r} is research-only and not runtime-implemented"
            )
        if _coerce_bool(params.get("use_volume_filter")):
            raise ValueError(
                "Market Impulse descendant volume filter is not runtime-supported: "
                "relative_volume features are unavailable in the live loop"
            )
        if entry_mode in {"same_bar_shallow_reclaim", "close_location_reclaim"}:
            return self._evaluate_single_bar_reclaim_entry(frame, deployment_id, params, entry_mode)
        if entry_mode == "continuation_confirmation":
            return self._evaluate_continuation_confirmation_entry(frame, deployment_id, params)
        raise ValueError(f"Unsupported Market Impulse entry_mode: {entry_mode!r}")

    def _evaluate_cross_reclaim_entry(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
    ) -> SignalDecision:
        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_col, regime_col = _feature_columns(params)
        entry_start, entry_end = _entry_window(params)

        timestamp = latest["timestamp"]
        bar_time_et = as_et_time(timestamp)

        reasons: list[str] = []
        if entry_start <= bar_time_et <= entry_end:
            reasons.append("time_window_ok")
        else:
            reasons.append("time_window_blocked")

        regime = str(latest[regime_col])
        vma = float(latest[vma_col])
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])

        long_signal = regime == "bullish" and low <= vma and close > vma
        short_signal = regime == "bearish" and high >= vma and close < vma

        signal = False
        direction: SignalDirection | None = None

        if entry_start <= bar_time_et <= entry_end:
            if long_signal and direction_filter in (None, "long"):
                signal = True
                direction = SignalDirection.LONG
                reasons.extend(["regime_bullish", "cross_and_reclaim_long"])
            elif short_signal and direction_filter in (None, "short"):
                signal = True
                direction = SignalDirection.SHORT
                reasons.extend(["regime_bearish", "cross_and_reclaim_short"])

        if not signal:
            reasons.append(_regime_reason(regime))

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                vma_col: vma,
                regime_col: regime,
            },
        )

    def _evaluate_single_bar_reclaim_entry(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        entry_mode: str,
    ) -> SignalDecision:
        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_col, regime_col = _feature_columns(params)
        entry_start, entry_end = _entry_window(params)
        max_vma_excursion_pct = _coerce_optional_float(params.get("max_vma_excursion_pct"))
        min_reclaim_margin_pct = float(params.get("min_reclaim_margin_pct", 0.0) or 0.0)
        min_close_location = _coerce_optional_float(params.get("min_close_location"))

        timestamp = latest["timestamp"]
        bar_time_et = as_et_time(timestamp)
        time_ok = entry_start <= bar_time_et <= entry_end

        regime = str(latest[regime_col])
        vma = float(latest[vma_col])
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])
        close_location = _close_location(high=high, low=low, close=close)

        reasons: list[str] = ["time_window_ok" if time_ok else "time_window_blocked"]

        long_ok = (
            time_ok
            and regime == "bullish"
            and low <= vma
            and close > vma
            and _reclaim_margin_ok("long", close=close, vma=vma, margin=min_reclaim_margin_pct)
            and _excursion_ok("long", high=high, low=low, vma=vma, max_pct=max_vma_excursion_pct)
            and _close_location_ok("long", close_location=close_location, min_close_location=min_close_location)
        )
        short_ok = (
            time_ok
            and regime == "bearish"
            and high >= vma
            and close < vma
            and _reclaim_margin_ok("short", close=close, vma=vma, margin=min_reclaim_margin_pct)
            and _excursion_ok("short", high=high, low=low, vma=vma, max_pct=max_vma_excursion_pct)
            and _close_location_ok("short", close_location=close_location, min_close_location=min_close_location)
        )

        signal = False
        direction: SignalDirection | None = None
        if long_ok and direction_filter in (None, "long"):
            signal = True
            direction = SignalDirection.LONG
            reasons.extend(["regime_bullish", f"{entry_mode}_long"])
        elif short_ok and direction_filter in (None, "short"):
            signal = True
            direction = SignalDirection.SHORT
            reasons.extend(["regime_bearish", f"{entry_mode}_short"])

        if not signal:
            reasons.append(_regime_reason(regime))

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features={
                "close": close,
                vma_col: vma,
                regime_col: regime,
                "close_location": close_location,
                "vma_excursion_pct": _vma_excursion_pct(high=high, low=low, vma=vma),
            },
        )

    def _evaluate_continuation_confirmation_entry(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
    ) -> SignalDecision:
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_col, regime_col = _feature_columns(params)
        entry_start, entry_end = _entry_window(params)
        max_vma_excursion_pct = _coerce_optional_float(params.get("max_vma_excursion_pct"))
        min_reclaim_margin_pct = float(params.get("min_reclaim_margin_pct", 0.0) or 0.0)
        confirmation_window_bars = int(params.get("confirmation_window_bars", 2))
        confirmation_type = _normalize_confirmation_type(params.get("confirmation_type"))
        confirmation_margin_pct = float(params.get("confirmation_margin_pct", 0.0) or 0.0)

        rows = frame.select(["timestamp", "high", "low", "close", vma_col, regime_col]).to_dicts()
        last_index = len(rows) - 1
        pending: dict[str, dict[str, Any] | None] = {"long": None, "short": None}
        confirmed_side: str | None = None
        confirmed_state: dict[str, Any] | None = None

        # Deterministic replay of the research state machine over the frame
        # prefix; only the latest bar's outcome is emitted (see mala_v2
        # ``MarketImpulseStrategy._continuation_confirmation_from_rows``).
        for index, row in enumerate(rows):
            raw_vma = row[vma_col]
            if raw_vma is None:
                continue
            vma = float(raw_vma)
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            regime = str(row[regime_col])
            bullish = regime == "bullish"
            bearish = regime == "bearish"
            bar_time_et = as_et_time(row["timestamp"])
            time_ok = entry_start <= bar_time_et <= entry_end

            row_confirmed_side: str | None = None
            row_confirmed_state: dict[str, Any] | None = None
            for side in ("long", "short"):
                state = pending[side]
                if state is None:
                    continue
                age = index - int(state["index"])
                regime_ok = bullish if side == "long" else bearish
                if age > confirmation_window_bars or not regime_ok:
                    pending[side] = None
                    continue
                if age <= 0:
                    continue
                if time_ok and _confirms_continuation(
                    side,
                    high=high,
                    low=low,
                    close=close,
                    vma=vma,
                    state=state,
                    confirmation_type=confirmation_type,
                    confirmation_margin_pct=confirmation_margin_pct,
                ):
                    row_confirmed_side = side
                    row_confirmed_state = dict(state, age=age)
                    pending[side] = None

            long_reclaim = (
                bullish
                and _excursion_ok("long", high=high, low=low, vma=vma, max_pct=max_vma_excursion_pct)
                and _reclaim_margin_ok("long", close=close, vma=vma, margin=min_reclaim_margin_pct)
                and low <= vma
            )
            short_reclaim = (
                bearish
                and _excursion_ok("short", high=high, low=low, vma=vma, max_pct=max_vma_excursion_pct)
                and _reclaim_margin_ok("short", close=close, vma=vma, margin=min_reclaim_margin_pct)
                and high >= vma
            )
            if long_reclaim:
                pending["long"] = {"index": index, "high": high, "low": low}
            if short_reclaim:
                pending["short"] = {"index": index, "high": high, "low": low}

            if index == last_index:
                confirmed_side = row_confirmed_side
                confirmed_state = row_confirmed_state

        latest = rows[-1]
        timestamp = latest["timestamp"]
        latest_time_ok = entry_start <= as_et_time(timestamp) <= entry_end
        regime = str(latest[regime_col])
        vma_value = latest[vma_col]

        reasons: list[str] = ["time_window_ok" if latest_time_ok else "time_window_blocked"]

        signal = False
        direction: SignalDirection | None = None
        if confirmed_side == "long" and direction_filter in (None, "long"):
            signal = True
            direction = SignalDirection.LONG
            reasons.extend(["regime_bullish", "continuation_confirmation_long"])
        elif confirmed_side == "short" and direction_filter in (None, "short"):
            signal = True
            direction = SignalDirection.SHORT
            reasons.extend(["regime_bearish", "continuation_confirmation_short"])

        if not signal:
            reasons.append(_regime_reason(regime))
            if pending["long"] is not None or pending["short"] is not None:
                reasons.append("awaiting_continuation_confirmation")

        features: dict[str, Any] = {
            "close": float(latest["close"]),
            vma_col: float(vma_value) if vma_value is not None else None,
            regime_col: regime,
        }
        if confirmed_state is not None:
            features["reclaim_bar_high"] = confirmed_state["high"]
            features["reclaim_bar_low"] = confirmed_state["low"]
            features["confirmation_age_bars"] = confirmed_state["age"]

        return SignalDecision(
            deployment_id=deployment_id,
            symbol=str(frame.tail(1).to_dicts()[0]["symbol"]),
            timestamp=timestamp,
            signal=signal,
            direction=direction,
            reason=reasons,
            features=features,
        )

    def evaluate_exit(
        self,
        frame: pl.DataFrame,
        deployment_id: str,
        params: dict[str, Any],
        position: TrackedPosition,
    ) -> ExitDecision:
        if frame.is_empty():
            raise ValueError("Cannot evaluate Market Impulse exit on an empty frame")

        latest = frame.tail(1).to_dicts()[0]
        direction_filter = str(params.get("direction", "")).strip().lower() or None
        vma_length = int(params.get("vma_length", 10))
        regime_timeframe = str(params.get("regime_timeframe", "1h"))
        timestamp = latest["timestamp"]
        vma_col = f"vma_{vma_length}"
        regime_col = f"impulse_regime_{regime_timeframe}"
        vma = float(latest[vma_col])
        close = float(latest["close"])
        regime = str(latest[regime_col])

        reasons: list[str] = []
        exit_now = False
        action = "hold"

        if direction_filter in (None, "short"):
            if close > vma:
                exit_now = True
                action = "square_off"
                reasons.append("vma_reclaim_exit")
            elif regime == "bullish":
                exit_now = True
                action = "square_off"
                reasons.append("regime_flip_bullish_exit")
        elif direction_filter == "long":
            if close < vma:
                exit_now = True
                action = "square_off"
                reasons.append("vma_loss_exit")
            elif regime == "bearish":
                exit_now = True
                action = "square_off"
                reasons.append("regime_flip_bearish_exit")

        if not reasons:
            reasons.append("hold_position")

        return ExitDecision(
            deployment_id=deployment_id,
            symbol=str(latest["symbol"]),
            timestamp=timestamp,
            exit=exit_now,
            action=action,
            reason=reasons,
            cancel_protection_orders=exit_now,
            features={
                vma_col: vma,
                regime_col: regime,
                "close": close,
                "position_option_symbol": position.option_symbol,
            },
        )


def _feature_columns(params: dict[str, Any]) -> tuple[str, str]:
    vma_length = int(params.get("vma_length", 10))
    regime_timeframe = str(params.get("regime_timeframe", "1h"))
    return f"vma_{vma_length}", f"impulse_regime_{regime_timeframe}"


def _entry_window(params: dict[str, Any]) -> tuple[time, time]:
    entry_buffer_minutes = int(params.get("entry_buffer_minutes", 3))
    entry_window_minutes = int(params.get("entry_window_minutes", 60))
    market_open_hour = int(params.get("market_open_hour", 9))
    market_open_minute = int(params.get("market_open_minute", 30))
    start_minutes = market_open_hour * 60 + market_open_minute + entry_buffer_minutes
    end_minutes = market_open_hour * 60 + market_open_minute + entry_window_minutes
    return time(start_minutes // 60, start_minutes % 60), time(end_minutes // 60, end_minutes % 60)


def _regime_reason(regime: str) -> str:
    if regime == "bullish":
        return "regime_bullish"
    if regime == "bearish":
        return "regime_bearish"
    return "regime_neutral"


def _close_location(*, high: float, low: float, close: float) -> float:
    """Mirror mala_v2 ``_close_location_expr``: range position, 0.5 on flat bars."""
    bar_range = high - low
    if bar_range > 0:
        return (close - low) / bar_range
    return 0.5


def _vma_excursion_pct(*, high: float, low: float, vma: float) -> float | None:
    """Mirror mala_v2 ``_vma_excursion_pct_expr`` (diagnostic column)."""
    if vma <= 0:
        return None
    long_depth = max(vma - low, 0.0)
    short_height = max(high - vma, 0.0)
    return max(long_depth, short_height) / vma


def _excursion_ok(side: str, *, high: float, low: float, vma: float, max_pct: float | None) -> bool:
    """Mirror mala_v2 ``_excursion_filter_expr``: cap the pierce depth beyond the VMA."""
    if max_pct is None:
        return True
    if vma <= 0:
        return False
    if side == "long":
        excursion = max(vma - low, 0.0)
    else:
        excursion = max(high - vma, 0.0)
    return (excursion / vma) <= float(max_pct)


def _reclaim_margin_ok(side: str, *, close: float, vma: float, margin: float) -> bool:
    """Mirror mala_v2 reclaim-margin expressions (strict inequalities)."""
    if side == "long":
        return close > vma * (1.0 + margin)
    return close < vma * (1.0 - margin)


def _close_location_ok(side: str, *, close_location: float, min_close_location: float | None) -> bool:
    """Mirror mala_v2 close-location gates; inert when the threshold is unset."""
    if min_close_location is None:
        return True
    if side == "long":
        return close_location >= float(min_close_location)
    return close_location <= (1.0 - float(min_close_location))


def _confirms_continuation(
    side: str,
    *,
    high: float,
    low: float,
    close: float,
    vma: float,
    state: dict[str, Any],
    confirmation_type: str,
    confirmation_margin_pct: float,
) -> bool:
    """Mirror mala_v2 ``_row_confirms_continuation``."""
    if confirmation_type == "break_reclaim_high_low":
        return high > float(state["high"]) if side == "long" else low < float(state["low"])
    if confirmation_type == "close_beyond_reclaim":
        return close > float(state["high"]) if side == "long" else close < float(state["low"])
    if side == "long":
        return close >= vma * (1.0 + confirmation_margin_pct)
    return close <= vma * (1.0 - confirmation_margin_pct)


def _normalize_entry_mode(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return BASELINE_ENTRY_MODE
    normalized = _ENTRY_MODE_ALIASES.get(text.lower(), text.lower())
    legal = {BASELINE_ENTRY_MODE, *IMPLEMENTED_DESCENDANT_ENTRY_MODES, *RESEARCH_ONLY_ENTRY_MODES}
    if normalized not in legal:
        raise ValueError(f"Unsupported Market Impulse entry_mode: {value!r}")
    return normalized


def _normalize_confirmation_type(value: Any) -> str:
    text = str(value or "").strip() or "break_reclaim_high_low"
    if text not in CONFIRMATION_TYPES:
        raise ValueError(
            f"Unsupported Market Impulse confirmation_type {value!r}; expected one of {CONFIRMATION_TYPES!r}"
        )
    return text


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _coerce_vwma_periods(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw_parts = value.replace(",", "_").replace("-", "_").split("_")
    elif isinstance(value, (list, tuple)):
        raw_parts = list(value)
    else:
        return None
    try:
        periods = tuple(int(part) for part in raw_parts if str(part).strip())
    except (TypeError, ValueError):
        return None
    if len(periods) < 3:
        return None
    return periods
