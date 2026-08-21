from __future__ import annotations

import hashlib
import json

from bhiksha.config.models import ExitSpec
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.ops.evidence_identity import effective_exit_policy_identity


def test_effective_identity_uses_frozen_policy_instead_of_executor_adapter() -> None:
    policy = {
        "policy_schema_version": "exit-policy.v1",
        "policy_id": "profile__trend_continuation.bhiksha.compat.v1",
        "no_progress_seconds": 900,
    }
    policy_hash = canonical_policy_hash(policy)
    exit_spec = ExitSpec(
        profile="manual_trigger_exit_v1",
        profile_exit_id="profile__trend_continuation",
        exit_policy_schema_version="exit-policy.v1",
        exit_policy_id=policy["policy_id"],
        exit_policy_hash=policy_hash,
        exit_policy_snapshot=policy,
    )

    assert effective_exit_policy_identity(exit_spec) == (
        "profile__trend_continuation.bhiksha.compat.v1",
        policy_hash,
    )


def test_effective_identity_preserves_legacy_adapter_fallback_as_one_pair() -> None:
    exit_spec = ExitSpec(profile="manual_trigger_exit_v1")
    expected_hash = hashlib.sha256(
        json.dumps(
            exit_spec.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert effective_exit_policy_identity(exit_spec) == (
        "manual_trigger_exit_v1",
        expected_hash,
    )


def test_effective_identity_does_not_mix_partial_policy_with_legacy_hash() -> None:
    exit_spec = ExitSpec(
        profile="strategy_managed_v1",
        exit_policy_id="incomplete.policy.v1",
    )

    policy_id, policy_hash = effective_exit_policy_identity(exit_spec)

    assert policy_id == "strategy_managed_v1"
    assert policy_hash != ""
