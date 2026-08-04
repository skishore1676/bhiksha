"""Pure evaluation of the kernel's typed chart trigger primitives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from mala_bhiksha_kernel import (
    ConditionType,
    ObservationWindow,
    TypedCondition,
)

from .models import CompletedBar, as_utc, timestamp_json


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    """A deterministic trigger result suitable for an event receipt."""

    condition_type: str
    triggered: bool
    within_window: bool
    reason: str
    evaluated_at: datetime
    bar_count: int
    completed_bar_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_type": self.condition_type,
            "triggered": self.triggered,
            "within_window": self.within_window,
            "reason": self.reason,
            "evaluated_at": timestamp_json(self.evaluated_at),
            "bar_count": self.bar_count,
            "completed_bar_count": self.completed_bar_count,
        }


def normalize_bars(
    bars: Sequence[CompletedBar | Mapping[str, Any]],
) -> list[CompletedBar]:
    """Convert, sort, and validate one completed-bar observation sequence."""

    normalized = [
        bar if isinstance(bar, CompletedBar) else CompletedBar.from_mapping(bar)
        for bar in bars
    ]
    timestamps = [bar.timestamp for bar in normalized]
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("completed bars must be strictly chronological and unique")
    bar_ids = [bar.bar_id for bar in normalized if bar.bar_id is not None]
    if len(bar_ids) != len(set(bar_ids)):
        raise ValueError("completed bar IDs must be unique")
    return normalized


def _false(
    condition: TypedCondition,
    *,
    now: datetime,
    reason: str,
    bars: Sequence[CompletedBar],
    within_window: bool = False,
) -> TriggerEvaluation:
    return TriggerEvaluation(
        condition_type=condition.condition_type.value,
        triggered=False,
        within_window=within_window,
        reason=reason,
        evaluated_at=now,
        bar_count=len(bars),
        completed_bar_count=len(bars),
    )


def _window_bounds(
    condition: TypedCondition,
    observation_window: ObservationWindow,
    now: datetime,
) -> tuple[datetime, datetime]:
    start = observation_window.start_at
    end = observation_window.end_at
    if condition.window_start is not None:
        start = max(start, condition.window_start)
    if condition.window_end is not None:
        end = min(end, condition.window_end)
    if condition.window_seconds is not None and condition.window_start is None:
        start = max(start, now - timedelta(seconds=condition.window_seconds))
    return start, end


def evaluate_condition(
    condition: TypedCondition,
    bars: Sequence[CompletedBar | Mapping[str, Any]],
    observation_window: ObservationWindow,
    *,
    evaluated_at: datetime | str | None = None,
) -> TriggerEvaluation:
    """Evaluate one supported condition using completed bars only.

    Crosses and reclaim/reject use adjacent completed closes.  Hold conditions
    require the requested number of latest closes to remain on the declared
    side.  Range breakouts use the completed close against the explicit bound
    plus/minus buffer.  No free-form prose is inspected here.
    """

    normalized = normalize_bars(bars)
    if evaluated_at is None:
        now = normalized[-1].timestamp if normalized else observation_window.start_at
    else:
        now = as_utc(evaluated_at)
    if now < observation_window.start_at:
        return _false(
            condition, now=now, reason="before_observation_window", bars=normalized
        )
    if now > observation_window.end_at:
        return _false(
            condition, now=now, reason="observation_window_expired", bars=normalized
        )

    start, end = _window_bounds(condition, observation_window, now)
    if end < start or now < start:
        return _false(
            condition, now=now, reason="condition_window_closed", bars=normalized
        )
    usable = [bar for bar in normalized if bar.timestamp <= now]
    current = [bar for bar in usable if start <= bar.timestamp <= min(end, now)]
    if not current:
        return _false(
            condition, now=now, reason="no_completed_bar_in_window", bars=usable
        )

    kind = condition.condition_type
    level = condition.level
    if kind in {
        ConditionType.CROSS_ABOVE,
        ConditionType.CROSS_BELOW,
        ConditionType.RECLAIM,
        ConditionType.REJECT,
    }:
        assert level is not None
        for bar in current:
            index = usable.index(bar)
            if index == 0:
                continue
            previous = usable[index - 1]
            if kind in {ConditionType.CROSS_ABOVE, ConditionType.RECLAIM}:
                hit = previous.close <= level and bar.close > level
            else:
                hit = previous.close >= level and bar.close < level
            if hit:
                return TriggerEvaluation(
                    condition_type=kind.value,
                    triggered=True,
                    within_window=True,
                    reason="typed_condition_satisfied",
                    evaluated_at=now,
                    bar_count=len(usable),
                    completed_bar_count=len(usable),
                )
        return _false(
            condition,
            now=now,
            reason="no_typed_cross_in_window",
            bars=usable,
            within_window=True,
        )

    if kind in {ConditionType.HOLD_ABOVE, ConditionType.HOLD_BELOW}:
        assert level is not None and condition.bars is not None
        if len(current) < condition.bars:
            return _false(
                condition,
                now=now,
                reason="insufficient_completed_bars",
                bars=usable,
                within_window=True,
            )
        tail = current[-condition.bars :]
        if kind is ConditionType.HOLD_ABOVE:
            hit = all(bar.close >= level for bar in tail)
        else:
            hit = all(bar.close <= level for bar in tail)
        return TriggerEvaluation(
            condition_type=kind.value,
            triggered=hit,
            within_window=True,
            reason="typed_condition_satisfied" if hit else "hold_side_not_maintained",
            evaluated_at=now,
            bar_count=len(usable),
            completed_bar_count=len(usable),
        )

    if kind is ConditionType.RANGE_BREAKOUT:
        assert (
            condition.range_low is not None
            and condition.range_high is not None
            and condition.buffer is not None
        )
        upper = condition.range_high + condition.buffer
        lower = condition.range_low - condition.buffer
        if condition.direction is not None and condition.direction.value == "long":
            hit = any(bar.close > upper for bar in current)
        elif condition.direction is not None and condition.direction.value == "short":
            hit = any(bar.close < lower for bar in current)
        else:
            hit = any(bar.close > upper or bar.close < lower for bar in current)
        return TriggerEvaluation(
            condition_type=kind.value,
            triggered=hit,
            within_window=True,
            reason="typed_condition_satisfied" if hit else "range_bounds_not_broken",
            evaluated_at=now,
            bar_count=len(usable),
            completed_bar_count=len(usable),
        )

    # The kernel enum makes this unreachable, but keeping the failure explicit
    # prevents a future enum extension from silently becoming a trigger.
    raise ValueError(f"unsupported trigger primitive: {kind!r}")


def evaluate_trigger(*args: Any, **kwargs: Any) -> TriggerEvaluation:
    """Compatibility spelling for callers that refer to conditions as triggers."""

    return evaluate_condition(*args, **kwargs)


__all__ = [
    "TriggerEvaluation",
    "evaluate_condition",
    "evaluate_trigger",
    "normalize_bars",
]
