"""Pure, mark-only exit-profile observations for the shadow lane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from mala_bhiksha_kernel import ExitProfile

from .models import OptionQuoteSnapshot, as_utc, timestamp_json


@dataclass(frozen=True, slots=True)
class ExitObservation:
    profile: ExitProfile
    status: str
    rule: str
    mark: float
    r: float
    elapsed_seconds: float
    state: Mapping[str, Any]
    reason: str

    @property
    def is_terminal(self) -> bool:
        return self.status == "exit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "status": self.status,
            "rule": self.rule,
            "mark": self.mark,
            "r": self.r,
            "elapsed_seconds": self.elapsed_seconds,
            "state": dict(self.state),
            "reason": self.reason,
            "mark_type": "counterfactual_mark_not_fill",
        }


_PROFILE_DEFAULT_TARGETS = {
    ExitProfile.FLASH_REVERSAL: 1.0,
    ExitProfile.EXHAUSTION_REVERSAL: 1.0,
    ExitProfile.TREND_CONTINUATION: 2.0,
    ExitProfile.RANGE_EXPANSION: 1.5,
}


def _get(policy: Any, name: str, default: Any = None) -> Any:
    if policy is None:
        return default
    if isinstance(policy, Mapping):
        return policy.get(name, default)
    return getattr(policy, name, default)


def _positive(value: Any, default: float) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return default
    return candidate if candidate > 0 else default


def _nonnegative(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return default
    return candidate if candidate >= 0 else default


def _optional_positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


def _time_from_policy(value: Any) -> time:
    if not isinstance(value, str):
        return time(15, 55)
    try:
        hour, minute = value.strip().split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return time(15, 55)


def evaluate_exit_profile(
    profile: ExitProfile | str,
    entry_quote: OptionQuoteSnapshot,
    current_quote: OptionQuoteSnapshot,
    *,
    entry_time: datetime | str,
    evaluated_at: datetime | str,
    management_policy: Any = None,
    prior_state: Mapping[str, Any] | None = None,
) -> ExitObservation:
    """Evaluate one profile against the same option mark path.

    This function never returns a fill and never accepts a broker object.  The
    result is a descriptive mark observation.  The caller labels the selected
    profile as primary and every other compatible profile as counterfactual.
    """

    try:
        selected = profile if isinstance(profile, ExitProfile) else ExitProfile(profile)
    except ValueError as exc:
        raise ValueError(f"unknown exit profile: {profile!r}") from exc
    if not entry_quote.eligible or not current_quote.eligible:
        raise ValueError("exit evaluation requires eligible option marks")
    if entry_quote.mark is None or current_quote.mark is None:
        raise ValueError("exit evaluation requires option marks")
    entry_at = as_utc(entry_time)
    now = as_utc(evaluated_at)
    if now < entry_at:
        raise ValueError("exit observation cannot precede synthetic entry")

    state = dict(prior_state or {})
    entry_mark = entry_quote.mark
    current_mark = current_quote.mark
    stop_pct = _positive(
        _get(
            management_policy,
            "initial_stop_pct",
            _get(management_policy, "option_stop_fallback_pct", 0.45),
        ),
        0.45,
    )
    r = (current_mark - entry_mark) / (entry_mark * stop_pct)
    peak_r = max(float(state.get("peak_r", r)), r)
    state["peak_r"] = peak_r
    elapsed = (now - entry_at).total_seconds()

    disaster_pct = _optional_positive(_get(management_policy, "premium_disaster_stop_pct"))
    if disaster_pct is not None and current_mark <= entry_mark * (1.0 - disaster_pct):
        return _result(selected, "exit", "disaster_stop", current_mark, r, elapsed, state, "premium_disaster_stop")
    if current_mark <= entry_mark * (1.0 - stop_pct):
        return _result(selected, "exit", "initial_stop", current_mark, r, elapsed, state, "premium_initial_stop")

    target_1 = _optional_positive(_get(management_policy, "target_1_r"))
    target_2 = _optional_positive(_get(management_policy, "target_2_r"))
    target_r = _optional_positive(_get(management_policy, "target_r"))
    if target_1 is None and target_2 is None and target_r is None:
        target_2 = _PROFILE_DEFAULT_TARGETS[selected]
    if target_2 is None and target_r is not None:
        target_2 = target_r
    target_1_quantity = _nonnegative(_get(management_policy, "target_1_quantity", 1.0), 1.0)
    if target_1 is not None and not state.get("target_1_hit") and r >= target_1:
        state["target_1_hit"] = True
        if target_2 is None or target_1_quantity >= 1.0:
            return _result(selected, "exit", "target_1", current_mark, r, elapsed, state, "primary_target_1")
        return _result(selected, "partial", "target_1_partial", current_mark, r, elapsed, state, "counterfactual_target_1_partial")
    if target_2 is not None and state.get("target_1_hit") and r >= target_2:
        return _result(selected, "exit", "target_2", current_mark, r, elapsed, state, "primary_target_2")
    if target_1 is None and target_2 is not None and r >= target_2:
        return _result(selected, "exit", "target", current_mark, r, elapsed, state, "profile_target")

    giveback_policy = str(
        _get(
            management_policy,
            "high_water_giveback_policy",
            _get(management_policy, "giveback_policy", "OFF"),
        )
        or "OFF"
    ).upper()
    tier_values = {
        "STRICT": (1.0, 0.33),
        "MODERATE": (1.25, 0.50),
        "LOOSE": (1.50, 0.66),
    }
    tier_arm, tier_fraction = tier_values.get(giveback_policy, (0.0, 0.0))
    giveback_fraction = _optional_positive(
        _get(management_policy, "giveback_retrace_fraction", tier_fraction or None)
    )
    giveback_arm = _optional_positive(_get(management_policy, "giveback_arm_r", tier_arm or None))
    if giveback_fraction is not None and giveback_arm is not None and peak_r >= giveback_arm:
        floor = peak_r * (1.0 - min(giveback_fraction, 1.0))
        if r <= floor:
            return _result(selected, "exit", "high_water_giveback", current_mark, r, elapsed, state, "profile_giveback")

    max_hold = _get(management_policy, "max_hold_seconds")
    if max_hold is not None:
        try:
            if elapsed >= float(max_hold):
                return _result(selected, "exit", "max_hold", current_mark, r, elapsed, state, "profile_max_hold")
        except (TypeError, ValueError):
            pass

    eod_flat = _get(management_policy, "eod_flat", True)
    if eod_flat:
        hard_flat = _time_from_policy(_get(management_policy, "hard_flat_time_et", "15:55"))
        if now.astimezone(ZoneInfo("America/New_York")).time() >= hard_flat:
            return _result(selected, "exit", "eod_flat", current_mark, r, elapsed, state, "profile_eod_flat")

    return _result(selected, "hold", "hold", current_mark, r, elapsed, state, "profile_hold")


def _result(
    profile: ExitProfile,
    status: str,
    rule: str,
    mark: float,
    r: float,
    elapsed: float,
    state: Mapping[str, Any],
    reason: str,
) -> ExitObservation:
    return ExitObservation(
        profile=profile,
        status=status,
        rule=rule,
        mark=mark,
        r=r,
        elapsed_seconds=elapsed,
        state=dict(state),
        reason=reason,
    )


__all__ = ["ExitObservation", "evaluate_exit_profile"]
