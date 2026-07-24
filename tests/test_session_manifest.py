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

    paths = write_session_manifest(snapshot, output_dir=tmp_path)
    assert paths.json_path.name == "session_manifest_2026-07-24_abc123.json"
    written = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert written == manifest
    assert "this file is a receipt" in paths.markdown_path.read_text(encoding="utf-8")
