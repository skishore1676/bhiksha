from __future__ import annotations

import json

import pytest

from bhiksha.execution.exit_policy import (
    canonical_policy_hash,
    canonical_policy_json,
    evaluate_risk_envelope,
)
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import load_risk_envelope_conformance_vectors  # noqa: E402


def test_local_policy_identity_matches_kernel_canonical_vector() -> None:
    vectors = load_risk_envelope_conformance_vectors()
    canonical = vectors["canonical_policy_hash_vector"]
    payload = json.loads(canonical["expected_canonical_json"])
    assert canonical_policy_json(payload) == canonical["expected_canonical_json"]
    assert canonical_policy_hash(payload) == canonical["expected_sha256"]


@pytest.mark.parametrize("variant_name", ["variant_a", "variant_b"])
def test_local_envelope_matches_kernel_observation_vectors(
    variant_name: str,
) -> None:
    vectors = load_risk_envelope_conformance_vectors()
    variant = next(
        item for item in vectors["variants"] if item["name"] == variant_name
    )
    policy = variant["policy"]
    for observation in variant["observations"]:
        actual = evaluate_risk_envelope(
            peak_r=observation["peak_r"],
            activation_r=policy["risk_envelope_activation_r"],
            target_1_r=policy["target_1_r"],
            initial_floor_r=policy["risk_envelope_initial_floor_r"],
            floor_at_t1_r=policy["risk_envelope_floor_at_t1_r"],
            curvature=policy["risk_envelope_curvature"],
        )
        assert actual == pytest.approx(observation["expected_floor_r"])


def test_envelope_rejects_non_finite_input() -> None:
    with pytest.raises(ValueError, match="finite"):
        evaluate_risk_envelope(
            peak_r=float("nan"),
            activation_r=0.25,
            target_1_r=1.0,
            initial_floor_r=-1.0,
            floor_at_t1_r=0.0,
            curvature=1.5,
        )
