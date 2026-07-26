from __future__ import annotations

import json

import pytest

from bhiksha.execution.exit_policy import (
    canonical_policy_hash,
    canonical_policy_json,
    compose_safety_stack_floor_r,
    evaluate_risk_envelope,
)
from bhiksha.ops.exit_edge_lab import build_risk_envelope_experiment
from bhiksha.ops.exit_edge_weekly import _canonical_catalog
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import (  # noqa: E402
    load_protective_floor_conformance_vectors,
    load_risk_envelope_conformance_vectors,
)


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


def test_local_policy_identity_excludes_labels_but_hashes_explicit_math() -> None:
    base = {
        "policy_schema_version": "exit-policy.v1",
        "policy_id": "exit.test.v1",
        "high_water_giveback_policy": "MODERATE",
        "source_config_id": "sheet-a",
        "giveback_arm_r": 1.25,
        "giveback_retrace_fraction": 0.5,
    }
    relabeled = {
        **base,
        "high_water_giveback_policy": "STRICT",
        "source_config_id": "sheet-b",
    }
    retuned = {**base, "giveback_arm_r": 0.75}

    assert canonical_policy_hash(base) == canonical_policy_hash(relabeled)
    assert canonical_policy_hash(base) != canonical_policy_hash(retuned)


def test_six_arm_adapter_and_weekly_catalog_match_kernel_exactly() -> None:
    vectors = load_protective_floor_conformance_vectors()
    control = {
        "policy_schema_version": "exit-policy.v1",
        "policy_id": "exit.premium_envelope.trend_continuation.control.v1",
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.6,
        "no_progress_seconds": 2700,
        "max_hold_seconds": 10800,
        "breakeven_after_t1": True,
        "eod_flat": True,
        "initial_stop_pct": 0.35,
        "premium_disaster_stop_pct": 0.45,
        "high_water_giveback_policy": "MODERATE",
        "giveback_arm_r": 1.25,
        "giveback_retrace_fraction": 0.5,
        "risk_envelope_enabled": False,
    }
    experiment = build_risk_envelope_experiment(
        control, control_policy_hash=canonical_policy_hash(control)
    )

    assert experiment["experiment_id"] == "trend-continuation-six-arm.v2"
    assert experiment["canonical_experiment_hash"] == (
        "f3f8a8dbb76952c2499f681266a8002d15360ea00406e92b6791791a8174b697"
    )
    assert {
        arm["candidate_id"]: arm["candidate_overlay_hash"]
        for arm in experiment["arms"]
    } == vectors["expected_candidate_overlay_hashes"]
    assert [
        arm["candidate_overlay"] for arm in experiment["arms"]
    ] == vectors["experiment"]["candidates"]
    assert _canonical_catalog()["sha256"] == (
        "3d0120bfc6736d53e3bf101bb2ba64deb939337f6e0add383ded46bfa04a5038"
    )

    wrong_core = {**control, "max_hold_seconds": 10_799}
    with pytest.raises(ValueError, match="exact kernel shared core"):
        build_risk_envelope_experiment(
            wrong_core,
            control_policy_hash=canonical_policy_hash(wrong_core),
        )


def test_live_safety_stack_math_matches_kernel_observation_vectors() -> None:
    vectors = load_protective_floor_conformance_vectors()
    observations = [
        row
        for row in vectors["observations"]
        if row["candidate_id"] == "safety_stack"
    ]
    for observation in observations:
        floor, components = compose_safety_stack_floor_r(
            confirmed_peak_r=observation["peak_r"],
            target_1_r=1.0,
            existing_floor_r=observation["previous_locked_floor_r"],
        )
        assert floor == pytest.approx(observation["expected_raw_floor_r"])
        assert set(components) == {
            "existing_floor_r",
            "envelope_floor_r",
            "giveback_floor_r",
        }
