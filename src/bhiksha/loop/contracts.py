"""Shared contract validation for Mala loop artifacts consumed by Bhiksha."""

from __future__ import annotations

from typing import Any


DEPLOYMENT_CANDIDATES_CONTRACT_NAME = "deployment_candidates"
PLAYBOOK_CATALOG_CONTRACT_NAME = "playbook_catalog"
LOOP_ARTIFACT_SCHEMA_VERSION = 2
ALLOWED_PLAYBOOK_COVERAGE_STATUSES = {
    "researched_with_survivors",
    "researched_no_survivors",
    "not_covered_by_enabled_family",
}


def validate_contract_metadata(
    payload: dict[str, Any],
    *,
    expected_contract_name: str,
) -> None:
    contract_name = payload.get("contract_name")
    if contract_name != expected_contract_name:
        raise ValueError(
            f"Unexpected contract_name {contract_name!r}; expected {expected_contract_name!r}"
        )
    schema_version = payload.get("schema_version")
    if schema_version != LOOP_ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version {schema_version!r}; "
            f"expected {LOOP_ARTIFACT_SCHEMA_VERSION}"
        )


__all__ = [
    "ALLOWED_PLAYBOOK_COVERAGE_STATUSES",
    "DEPLOYMENT_CANDIDATES_CONTRACT_NAME",
    "LOOP_ARTIFACT_SCHEMA_VERSION",
    "PLAYBOOK_CATALOG_CONTRACT_NAME",
    "validate_contract_metadata",
]
