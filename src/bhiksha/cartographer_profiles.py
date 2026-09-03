"""Resolve Cartographer profile snapshots from the operator Google Sheet.

Cartographer chooses a profile slug. ``Operator_Defaults_v1`` owns the
adjustable option expression and requested premium for that profile. Bhiksha
freezes the resolved values into each manual-entry row so compilation and
later evidence can prove exactly what was executed without creating a second
code-owned operator surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

PROFILE_SECTION_PREFIX = "profile__"


def canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _profile_section(slug: str) -> str:
    return PROFILE_SECTION_PREFIX + slug.strip().lower()


def _number(value: Any, *, field: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise TypeError(f"operator profile {field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"operator profile {field} must be numeric") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"operator profile {field} must be at least {minimum}")
    return result


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    number = _number(value, field=field, minimum=float(minimum))
    if not number.is_integer():
        raise ValueError(f"operator profile {field} must be an integer")
    return int(number)


def _boolean(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"operator profile {field} must be boolean")


def _text(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"operator profile {field} is required")
    return normalized


def _resolved_operator_profile(
    slug: str, operator_defaults: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    section_name = _profile_section(slug)
    global_defaults = {
        key: value
        for key, value in operator_defaults.items()
        if not isinstance(value, Mapping)
    }
    section = operator_defaults.get(section_name)
    if not isinstance(section, Mapping):
        raise TypeError(
            f"Operator_Defaults_v1 is missing required section {section_name!r}"
        )
    return section_name, {**global_defaults, **dict(section)}


def _management_policy(slug: str, values: Mapping[str, Any]) -> dict[str, Any]:
    if slug != "TREND_CONTINUATION":
        raise ValueError(f"unknown Cartographer profile slug: {slug!r}")
    initial_stop_pct = _number(
        values.get("initial_stop_pct"), field="initial_stop_pct", minimum=0.01
    )
    disaster_stop_pct = _number(
        values.get("premium_disaster_stop_pct"),
        field="premium_disaster_stop_pct",
        minimum=0.01,
    )
    target_1_r = _number(values.get("target_1_r"), field="target_1_r", minimum=0.01)
    target_2_r = _number(values.get("target_2_r"), field="target_2_r", minimum=0.01)
    target_1_quantity = _number(
        values.get("target_1_quantity_pct"),
        field="target_1_quantity_pct",
        minimum=0.01,
    )
    if initial_stop_pct > 1 or disaster_stop_pct > 1 or target_1_quantity > 1:
        raise ValueError("operator profile percentages must be decimals between 0 and 1")
    if target_2_r < target_1_r:
        raise ValueError("operator profile target_2_r cannot be below target_1_r")
    hard_flat_time_et = _text(values.get("hard_flat_time_et"), field="hard_flat_time_et")
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", hard_flat_time_et) is None:
        raise ValueError("operator profile hard_flat_time_et must use HH:MM")
    giveback_policy = _text(
        values.get("high_water_giveback_policy"),
        field="high_water_giveback_policy",
    ).upper()
    if giveback_policy not in {"TIGHT", "MODERATE", "WIDE"}:
        raise ValueError(
            "operator profile high_water_giveback_policy must be TIGHT, MODERATE, or WIDE"
        )
    management = {
        "policy_id": "profile__trend_continuation",
        "stop_family": "entry_bar_failure",
        "stop_anchor": "underlying_entry_bar_failure",
        "exit_family": "profile_staged_r",
        "target_model": "profile_staged_r",
        "target_r": target_2_r,
        "hard_flat_time_et": hard_flat_time_et,
        "option_stop_fallback_pct": disaster_stop_pct,
        "target_order_mode": "virtual_or_broker",
        "target_1_r": target_1_r,
        "target_2_r": target_2_r,
        "target_1_quantity": target_1_quantity,
        "initial_stop_pct": initial_stop_pct,
        "premium_disaster_stop_pct": disaster_stop_pct,
        "no_progress_seconds": _integer(
            values.get("no_progress_minutes"), field="no_progress_minutes", minimum=1
        ) * 60,
        "max_hold_seconds": _integer(
            values.get("max_hold_minutes"), field="max_hold_minutes", minimum=1
        ) * 60,
        "high_water_giveback_policy": giveback_policy,
        "breakeven_after_t1": _boolean(
            values.get("breakeven_after_t1"), field="breakeven_after_t1"
        ),
        "eod_flat": _boolean(values.get("eod_flat"), field="eod_flat"),
        "parameters": {
            "profile_owner": "market_cartographer",
            "no_progress_favorable_floor_r": _number(
                values.get("no_progress_favorable_floor_r"),
                field="no_progress_favorable_floor_r",
            ),
        },
    }
    management["management_hash"] = canonical_hash(management)
    return management


def profile_bundle(
    slug: str, operator_defaults: Mapping[str, Any]
) -> dict[str, Any]:
    """Return one content-bound snapshot of Sheet-owned profile inputs."""

    normalized_slug = slug.strip().upper()
    if normalized_slug != "TREND_CONTINUATION":
        raise ValueError(f"unknown Cartographer profile slug: {slug!r}")
    section_name, values = _resolved_operator_profile(
        normalized_slug, operator_defaults
    )
    dte_min = _integer(values.get("dte_min"), field="dte_min")
    dte_max = _integer(values.get("dte_max"), field="dte_max")
    if dte_min > dte_max:
        raise ValueError("operator profile dte_min cannot exceed dte_max")
    delta_min = _number(values.get("delta_min"), field="delta_min")
    delta_max = _number(values.get("delta_max"), field="delta_max")
    if delta_min > delta_max or delta_max > 1.0:
        raise ValueError(
            "operator profile delta range must satisfy 0 <= min <= max <= 1"
        )
    spread = _number(
        values.get("max_bid_ask_spread_pct"),
        field="max_bid_ask_spread_pct",
    )
    if spread > 1.0:
        raise ValueError("operator profile max_bid_ask_spread_pct cannot exceed 1")
    fallback = str(values.get("dte_fallback_policy") or "").strip().lower()
    if fallback not in {"strict", "allow_nearest_after"}:
        raise ValueError(
            "operator profile dte_fallback_policy must be strict or allow_nearest_after"
        )
    execution = {
        "schema": "bhiksha.cartographer_option_selection.v1",
        "selection_id": f"operator_sheet:{section_name}",
        "dte_min": dte_min,
        "dte_max": dte_max,
        "dte_fallback_policy": fallback,
        "target_abs_delta_min": delta_min,
        "target_abs_delta_max": delta_max,
        "min_open_interest": _integer(
            values.get("min_open_interest"), field="min_open_interest"
        ),
        "max_bid_ask_spread_pct": spread,
        "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
        "max_contracts": _integer(
            values.get("max_contracts"), field="max_contracts", minimum=1
        ),
    }
    execution["selection_hash"] = canonical_hash(execution)
    body: dict[str, Any] = {
        "bundle_id": f"operator_sheet:{section_name}",
        "profile_slug": normalized_slug,
        "operator_section": section_name,
        "execution": execution,
        "management": _management_policy(normalized_slug, values),
        "requested_max_trade_premium_usd": _number(
            values.get("max_trade_premium_usd"),
            field="max_trade_premium_usd",
            minimum=0.01,
        ),
    }
    body["bundle_hash"] = canonical_hash(body)
    return body


def validate_profile_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    expected = canonical_hash(
        {key: value for key, value in bundle.items() if key != "bundle_hash"}
    )
    if bundle.get("bundle_hash") != expected:
        raise ValueError("Cartographer profile snapshot hash mismatch")
    execution = bundle.get("execution")
    management = bundle.get("management")
    if not isinstance(execution, Mapping) or not isinstance(management, Mapping):
        raise TypeError("Cartographer profile snapshot sections are required")
    if execution.get("selection_hash") != canonical_hash(
        {key: value for key, value in execution.items() if key != "selection_hash"}
    ):
        raise ValueError("Cartographer option selection hash mismatch")
    if management.get("management_hash") != canonical_hash(
        {key: value for key, value in management.items() if key != "management_hash"}
    ):
        raise ValueError("Cartographer management policy hash mismatch")
    return json.loads(json.dumps(dict(bundle)))


__all__ = [
    "PROFILE_SECTION_PREFIX",
    "canonical_hash",
    "profile_bundle",
    "validate_profile_bundle",
]
