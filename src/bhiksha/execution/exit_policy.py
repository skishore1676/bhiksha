"""Local Exit Engine V2 conformance implementation.

Bhiksha production does not assume the sibling kernel is importable at runtime.
This module implements the kernel's ``exit-policy.v1`` canonical JSON/hash and
pure Dynamic Risk Envelope contract. Tests run the packaged kernel vectors
against this implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

EXIT_POLICY_SCHEMA_VERSION = "exit-policy.v1"
RISK_ENVELOPE_CONFORMANCE_VERSION = "dynamic-risk-envelope.v1"
NON_SEMANTIC_POLICY_FIELDS = frozenset(
    {
        "high_water_giveback_policy",
        "source_config_id",
    }
)


def canonical_policy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in NON_SEMANTIC_POLICY_FIELDS
    }


def canonical_policy_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_policy_payload(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_policy_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_policy_json(payload).encode("utf-8")).hexdigest()


def evaluate_risk_envelope(
    *,
    peak_r: float,
    activation_r: float,
    target_1_r: float,
    initial_floor_r: float,
    floor_at_t1_r: float,
    curvature: float,
) -> float:
    values = {
        "peak_r": peak_r,
        "activation_r": activation_r,
        "target_1_r": target_1_r,
        "initial_floor_r": initial_floor_r,
        "floor_at_t1_r": floor_at_t1_r,
        "curvature": curvature,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite real number")
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if target_1_r <= activation_r:
        raise ValueError("target_1_r must be greater than activation_r")
    if curvature <= 0:
        raise ValueError("curvature must be greater than zero")
    if floor_at_t1_r < initial_floor_r:
        raise ValueError("floor_at_t1_r must not be below initial_floor_r")
    if floor_at_t1_r > target_1_r:
        raise ValueError("floor_at_t1_r must not exceed target_1_r")
    if peak_r <= activation_r:
        return float(initial_floor_r)
    if peak_r >= target_1_r:
        return float(floor_at_t1_r)
    progress = (peak_r - activation_r) / (target_1_r - activation_r)
    candidate = initial_floor_r + (
        (floor_at_t1_r - initial_floor_r) * (progress**curvature)
    )
    return float(min(floor_at_t1_r, max(initial_floor_r, candidate)))


def compose_safety_stack_floor_r(
    *,
    confirmed_peak_r: float,
    target_1_r: float,
    existing_floor_r: float,
    envelope_activation_r: float = 0.25,
    envelope_curvature: float = 1.5,
    envelope_initial_floor_r: float = -1.0,
    envelope_floor_at_t1_r: float = 0.0,
    giveback_arm_r: float = 0.75,
    giveback_retrace_fraction: float = 0.60,
) -> tuple[float, dict[str, float | None]]:
    """Return the tightest non-loosening pre-T1 safety floor.

    Increment 2's sole live candidate is ``safety_stack``: the current broker
    stop, Envelope A, and the canonical 0.75R/60% giveback floor.  The maximum
    is protective for a long-premium position.  This function is pure so the
    same vectors can be shared by replay, runtime, and audit tests.
    """

    envelope_floor = evaluate_risk_envelope(
        peak_r=confirmed_peak_r,
        activation_r=envelope_activation_r,
        target_1_r=target_1_r,
        initial_floor_r=envelope_initial_floor_r,
        floor_at_t1_r=envelope_floor_at_t1_r,
        curvature=envelope_curvature,
    )
    giveback_floor = (
        confirmed_peak_r * (1.0 - giveback_retrace_fraction)
        if confirmed_peak_r >= giveback_arm_r
        else None
    )
    candidates = [float(existing_floor_r), float(envelope_floor)]
    if giveback_floor is not None:
        candidates.append(float(giveback_floor))
    return max(candidates), {
        "existing_floor_r": float(existing_floor_r),
        "envelope_floor_r": float(envelope_floor),
        "giveback_floor_r": (
            float(giveback_floor) if giveback_floor is not None else None
        ),
    }
