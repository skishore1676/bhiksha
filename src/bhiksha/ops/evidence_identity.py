"""Canonical evidence identities derived from resolved deployment policy."""

from __future__ import annotations

import hashlib
import json

from bhiksha.config.models import ExitSpec


def effective_exit_policy_identity(exit_spec: ExitSpec) -> tuple[str, str]:
    """Return one internally consistent policy ID/hash pair for trade evidence.

    Exit Engine V2 deployments carry an authoritative frozen policy identity.
    Older deployments do not, so they retain the historical adapter identity
    and whole-exit-spec hash for backward compatibility.
    """
    if exit_spec.exit_policy_id and exit_spec.exit_policy_hash:
        return exit_spec.exit_policy_id, exit_spec.exit_policy_hash

    legacy_hash = hashlib.sha256(
        json.dumps(
            exit_spec.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return exit_spec.profile, legacy_hash
