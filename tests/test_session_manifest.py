from __future__ import annotations

import json

from bhiksha.config.models import (
    DeploymentManifest,
    ExecutionSpec,
    ExitSpec,
    RiskSpec,
    StrategySpec,
)
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.ops.session_manifest import (
    build_session_manifest,
    effective_exit_policy_records,
    write_session_manifest,
)


def _deployment() -> DeploymentManifest:
    policy = {
        "policy_schema_version": "exit-policy.v1",
        "policy_id": "exit.test.v1",
        "giveback_arm_r": 1.25,
        "giveback_retrace_fraction": 0.5,
    }
    return DeploymentManifest(
        deployment_id="dep",
        symbol="QQQ",
        strategy=StrategySpec(key="test"),
        execution=ExecutionSpec(
            profile="test",
            runtime_mode="shadow",
            shadow_only=True,
        ),
        risk=RiskSpec(profile="test"),
        exit=ExitSpec(
            profile_exit_id="TREND_CONTINUATION",
            exit_policy_schema_version="exit-policy.v1",
            exit_policy_id="exit.test.v1",
            exit_policy_hash=canonical_policy_hash(policy),
            exit_policy_snapshot=policy,
            exit_policy_provenance={"resolution": "source_explicit"},
            giveback_arm_r=1.25,
            giveback_retrace_fraction=0.5,
        ),
    )


def test_effective_policy_receipt_verifies_hash_without_new_authority() -> None:
    record = effective_exit_policy_records([_deployment()])[0]
    assert record["policy_hash_verified"] is True
    assert record["resolution_status"] == "source_explicit"
    assert record["policy"]["policy_id"] == "exit.test.v1"
    assert record["risk_envelope_live"] == {
        "mode": "off",
        "candidate_id": None,
        "candidate_overlay_hash": None,
        "authorization_id": None,
        "start_at": None,
        "expires_at": None,
        "authorized_deployment_id": None,
        "authorized_symbol": None,
        "authorized_active_plan_id": None,
        "rollback_action": None,
        "max_premium_cap_fraction": None,
        "max_quote_age_ms": 2000,
        "max_spread_pct": 0.15,
    }


def test_session_manifest_is_projection_of_startup_snapshot(tmp_path) -> None:
    snapshot = {
        "active_plan": {
            "active_plan_id": "active_plan_2026-07-24",
            "trading_date": "2026-07-24",
            "source": {"name": "test"},
            "suppressed": [{"row_id": "bad", "reason": "invalid"}],
        },
        "deployment_selection": {
            "mode": "active_plan",
            "active_plan_id": "active_plan_2026-07-24",
        },
        "config_fingerprint": "abc123",
        "code_version": {"git_sha": "deadbeef"},
        "effective_exit_policies": effective_exit_policy_records([_deployment()]),
    }
    manifest = build_session_manifest(snapshot)
    assert manifest["artifact_role"] == "generated_receipt_only"
    assert manifest["configuration_authority"] == ["active_plan", "startup_config"]
    assert manifest["session_manifest_id"] == "active_plan_2026-07-24:abc123"
    assert manifest["rejected_or_suppressed_inputs"][0]["row_id"] == "bad"
    assert manifest["risk_envelope_canaries"] == []

    paths = write_session_manifest(snapshot, output_dir=tmp_path)
    assert paths.json_path.name == "session_manifest_2026-07-24_abc123.json"
    written = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert written == manifest
    assert "this file is a receipt" in paths.markdown_path.read_text(encoding="utf-8")


def test_session_manifest_disarms_expired_or_not_yet_valid_canary() -> None:
    base_live = {
        "mode": "canary",
        "candidate_id": "safety_stack",
        "authorization_id": "auth",
    }
    snapshot = {
        "deployment_selection": {"active_plan_id": "plan"},
        "config_fingerprint": "startup",
        "risk_envelope_authorization_fingerprint": "f" * 64,
        "effective_exit_policies": [
            {
                "deployment_id": "expired",
                "risk_envelope_live": {
                    **base_live,
                    "start_at": "2026-07-01T00:00:00+00:00",
                    "expires_at": "2026-07-02T00:00:00+00:00",
                },
            },
            {
                "deployment_id": "future",
                "risk_envelope_live": {
                    **base_live,
                    "start_at": "2099-07-01T00:00:00+00:00",
                    "expires_at": "2099-07-02T00:00:00+00:00",
                },
            },
        ],
    }

    manifest = build_session_manifest(snapshot)
    states = {
        item["deployment_id"]: item["state"]
        for item in manifest["risk_envelope_canaries"]
    }
    assert states == {
        "expired": "disarmed_authorization_expired",
        "future": "disarmed_authorization_not_yet_valid",
    }
    assert all(
        item["startup_authorization_fingerprint"] == "f" * 64
        for item in manifest["risk_envelope_canaries"]
    )


def test_session_manifest_rollback_latch_overrides_static_armed_state() -> None:
    snapshot = {
        "deployment_selection": {"active_plan_id": "plan"},
        "config_fingerprint": "startup",
        "risk_envelope_authorization_fingerprint": "f" * 64,
        "effective_exit_policies": [
            {
                "deployment_id": "iwm-canary",
                "risk_envelope_live": {
                    "mode": "canary",
                    "candidate_id": "safety_stack",
                    "authorization_id": "auth",
                    "start_at": "2026-07-01T00:00:00+00:00",
                    "expires_at": "2099-07-02T00:00:00+00:00",
                },
            }
        ],
    }
    latch = {
        "deployment_id": "iwm-canary",
        "reason": "stop_handoff_unproved",
        "latched_at": "2026-07-25T20:00:00+00:00",
    }

    manifest = build_session_manifest(
        snapshot,
        rollback_latches=[latch],
    )

    canary = manifest["risk_envelope_canaries"][0]
    assert canary["state"] == "disarmed_rollback_latched"
    assert canary["rollback_latch"] == latch
    assert manifest["risk_envelope_rollback_latches"] == [latch]
