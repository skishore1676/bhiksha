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


def canonical_policy_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
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
