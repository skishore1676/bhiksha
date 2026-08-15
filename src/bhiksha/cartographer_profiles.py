"""Frozen compiler-side policy bundles for Cartographer-originated shadow rows.

The projector may display these values, but this registry is the authority that
binds the option expression, management policy, and requested risk preference.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bundle() -> dict[str, Any]:
    execution = {
        "schema": "bhiksha.cartographer_option_selection.v1",
        "selection_id": "cartographer_trend_continuation_options_v1",
        "dte_min": 3,
        "dte_max": 7,
        "dte_fallback_policy": "strict",
        "target_abs_delta_min": 0.15,
        "target_abs_delta_max": 0.35,
        "min_open_interest": 100,
        "max_bid_ask_spread_pct": 0.20,
        "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
        "max_contracts": 1,
    }
    execution["selection_hash"] = canonical_hash(execution)
    management = {
        "policy_id": "profile__trend_continuation",
        "stop_family": "entry_bar_failure",
        "stop_anchor": "underlying_entry_bar_failure",
        "exit_family": "profile_staged_r",
        "target_model": "profile_staged_r",
        "target_r": 2.0,
        "hard_flat_time_et": "15:55",
        "option_stop_fallback_pct": 0.35,
        "target_order_mode": "virtual_or_broker",
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.6,
        "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.35,
        "no_progress_seconds": 2700,
        "max_hold_seconds": 10800,
        "high_water_giveback_policy": "MODERATE",
        "breakeven_after_t1": True,
        "eod_flat": True,
        "parameters": {
            "profile_owner": "market_cartographer",
            "no_progress_favorable_floor_r": 0.25,
        },
    }
    management["management_hash"] = canonical_hash(management)
    body: dict[str, Any] = {
        "bundle_id": "cartographer_trend_continuation_v1",
        "profile_slug": "TREND_CONTINUATION",
        "execution": execution,
        "management": management,
        "requested_max_trade_premium_usd": 500.0,
    }
    body["bundle_hash"] = canonical_hash(body)
    return body


_BUNDLES = {"TREND_CONTINUATION": _bundle()}


def profile_bundle(slug: str) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(_BUNDLES[slug]))
    except KeyError as exc:
        raise ValueError(f"unknown Cartographer profile slug: {slug!r}") from exc


def validate_profile_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    expected = canonical_hash({key: value for key, value in bundle.items() if key != "bundle_hash"})
    if bundle.get("bundle_hash") != expected:
        raise ValueError("Cartographer profile bundle hash mismatch")
    execution = bundle.get("execution")
    management = bundle.get("management")
    if not isinstance(execution, Mapping) or not isinstance(management, Mapping):
        raise ValueError("Cartographer profile bundle sections are required")
    if execution.get("selection_hash") != canonical_hash(
        {key: value for key, value in execution.items() if key != "selection_hash"}
    ):
        raise ValueError("Cartographer option selection hash mismatch")
    if management.get("management_hash") != canonical_hash(
        {key: value for key, value in management.items() if key != "management_hash"}
    ):
        raise ValueError("Cartographer management policy hash mismatch")
    return dict(bundle)


__all__ = ["canonical_hash", "profile_bundle", "validate_profile_bundle"]
