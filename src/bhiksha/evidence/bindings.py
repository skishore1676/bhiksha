"""Bind immutable Mala experiment packets to exact compiled deployments.

Bindings are observational metadata. They never grant, revoke, or change a
row's authorization mode, risk, strategy, execution, or exit settings. A
shadow row with an incompatible binding is suppressed because running an
unattributable observation has no evidentiary value. A live row remains owned
by the operator Sheet and its execution contract; the compiler preserves that
authorization while quarantining the incompatible observation identity so it
cannot enter decision-grade reporting.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from bhiksha.config.models import DeploymentManifest


BINDING_REGISTRY_V1 = "bhiksha.evidence_binding_registry.v1"
BINDING_V1 = "bhiksha.evidence_binding.v1"
DEFAULT_EVIDENCE_BINDINGS_PATH = Path("config/evidence_bindings_v1.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def load_evidence_bindings(path: str | Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != BINDING_REGISTRY_V1:
        raise ValueError("unsupported evidence-binding registry schema")
    expected_registry_sha = canonical_sha256(
        {key: value for key, value in payload.items() if key != "registry_sha256"}
    )
    if payload.get("registry_sha256") != expected_registry_sha:
        raise ValueError("evidence-binding registry digest mismatch")
    index: dict[str, dict[str, Any]] = {}
    for raw in payload.get("bindings") or []:
        binding = dict(raw)
        _validate_binding(binding)
        strategy_id = str(binding["strategy_id"])
        if strategy_id in index:
            raise ValueError(f"duplicate evidence binding for {strategy_id!r}")
        index[strategy_id] = binding
    return index


def apply_evidence_binding(
    deployment: DeploymentManifest,
    *,
    strategy_id: str,
    authorization_mode: str,
    bindings: Mapping[str, Mapping[str, Any]],
) -> DeploymentManifest:
    raw = bindings.get(strategy_id)
    if raw is None:
        return deployment
    binding = dict(raw)
    _validate_binding(binding)
    if deployment.symbol != str(binding["symbol"]).upper():
        raise ValueError(f"evidence binding {strategy_id!r} symbol mismatch")
    direction = str(deployment.strategy.params.get("direction") or "").lower()
    if direction != str(binding["direction"]).lower():
        raise ValueError(f"evidence binding {strategy_id!r} direction mismatch")
    allowed_modes = {str(value).lower() for value in binding["allowed_authorization_modes"]}
    if authorization_mode.lower() not in allowed_modes:
        raise ValueError(
            f"evidence binding {strategy_id!r} does not allow authorization_mode={authorization_mode!r}"
        )
    option_contract = dict(binding["declared_option_selection_contract"])
    _validate_option_contract(deployment, option_contract, strategy_id=strategy_id)
    metadata = dict(deployment.source.metadata)
    existing_packet_id = metadata.get("evidence_packet_id")
    observation_only = False
    if existing_packet_id and existing_packet_id != binding["evidence_packet_id"]:
        if not metadata.get("authorization_sha256"):
            raise ValueError(
                f"evidence binding {strategy_id!r} conflicts with an unowned packet identity"
            )
        # The historical packet is part of the signed canary authorization and
        # must remain byte-for-byte intact.  Put the prospective experiment in
        # a separate observation namespace; runtime attribution explicitly
        # prefers this namespace, while authorization validation continues to
        # verify the original complete deployment contract.
        observation_only = True
    additions = {
        "evidence_binding_schema_version": BINDING_V1,
        "evidence_binding_sha256": binding["binding_sha256"],
        "run_id": binding["run_id"],
        "research_run_id": binding["run_id"],
        **(
            {
                "observation_evidence_packet_id": binding["evidence_packet_id"],
                "observation_evidence_artifact_sha256": binding["artifact_sha256"],
                "observation_evidence_artifact_uri": binding["artifact_uri"],
            }
            if observation_only
            else {
                "evidence_packet_id": binding["evidence_packet_id"],
                "artifact_sha256": binding["artifact_sha256"],
                "artifact_uri": binding["artifact_uri"],
            }
        ),
        "experiment_id": binding["experiment_id"],
        "cohort_id": binding["cohort_id"],
        "cohort_contract_sha256": binding["cohort_contract_sha256"],
        "declared_option_selection_contract_id": option_contract["contract_id"],
        "declared_option_selection_contract_sha256": option_contract["contract_sha256"],
        "declared_option_selection_contract": option_contract,
        "authorization_identity_status": (
            "compiled_observation_only"
            if observation_only
            else "content_addressed"
            if metadata.get("authorization_sha256")
            else (
                "compiled_observation_only"
                if authorization_mode.lower() == "live"
                else "shadow_observation"
            )
        ),
    }
    conflicts = [
        key
        for key, value in additions.items()
        if key in metadata and metadata[key] not in (None, value)
    ]
    if conflicts:
        raise ValueError(
            f"evidence binding {strategy_id!r} conflicts with source metadata: "
            + ", ".join(sorted(conflicts))
        )
    metadata.update(additions)
    updated = deployment.model_copy(
        update={
            "source": deployment.source.model_copy(update={"metadata": metadata})
        }
    )
    payload = updated.model_dump(mode="json")
    payload["source"]["metadata"].pop("deployment_contract_sha256", None)
    metadata["deployment_contract_sha256"] = canonical_sha256(payload)
    return updated.model_copy(
        update={
            "source": updated.source.model_copy(update={"metadata": metadata})
        }
    )


def build_registry_payload(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for raw in bindings:
        binding = dict(raw)
        binding["schema_version"] = BINDING_V1
        binding["binding_sha256"] = canonical_sha256(
            {key: value for key, value in binding.items() if key != "binding_sha256"}
        )
        _validate_binding(binding)
        normalized.append(binding)
    payload: dict[str, Any] = {
        "schema_version": BINDING_REGISTRY_V1,
        "bindings": sorted(normalized, key=lambda item: item["strategy_id"]),
    }
    payload["registry_sha256"] = canonical_sha256(payload)
    return payload


def _validate_binding(binding: Mapping[str, Any]) -> None:
    if binding.get("schema_version") != BINDING_V1:
        raise ValueError("unsupported evidence binding schema")
    for field in (
        "strategy_id",
        "symbol",
        "direction",
        "run_id",
        "experiment_id",
        "cohort_id",
        "artifact_uri",
    ):
        if not isinstance(binding.get(field), str) or not str(binding[field]).strip():
            raise ValueError(f"evidence binding missing {field}")
    for field in (
        "binding_sha256",
        "evidence_packet_id",
        "artifact_sha256",
        "cohort_contract_sha256",
    ):
        if _SHA256.fullmatch(str(binding.get(field) or "")) is None:
            raise ValueError(f"evidence binding has invalid {field}")
    if not str(binding["artifact_uri"]).startswith(
        f"mala-evidence://sha256/{binding['evidence_packet_id']}/"
    ):
        raise ValueError("evidence binding artifact URI is not packet-bound")
    if not isinstance(binding.get("allowed_authorization_modes"), list) or not binding[
        "allowed_authorization_modes"
    ]:
        raise ValueError("evidence binding requires allowed_authorization_modes")
    option_contract = binding.get("declared_option_selection_contract")
    if not isinstance(option_contract, Mapping):
        raise ValueError("evidence binding requires declared_option_selection_contract")
    expected = canonical_sha256(
        {key: value for key, value in binding.items() if key != "binding_sha256"}
    )
    if binding.get("binding_sha256") != expected:
        raise ValueError("evidence binding digest mismatch")


def _validate_option_contract(
    deployment: DeploymentManifest,
    contract: Mapping[str, Any],
    *,
    strategy_id: str,
) -> None:
    if contract.get("schema_version") != "mala.option_selection_contract.v1":
        raise ValueError(f"evidence binding {strategy_id!r} option contract schema mismatch")
    contract_sha = str(contract.get("contract_sha256") or "")
    expected_sha = canonical_sha256(
        {key: value for key, value in contract.items() if key != "contract_sha256"}
    )
    if contract_sha != expected_sha:
        raise ValueError(f"evidence binding {strategy_id!r} option contract digest mismatch")
    parameters = contract.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError(f"evidence binding {strategy_id!r} option parameters missing")
    actual = deployment.execution.model_dump(mode="json")
    actual.update(
        {
            "long_signal_contract_type": deployment.execution.option_mapping.get(
                "long_signal", "CALL"
            ),
            "short_signal_contract_type": deployment.execution.option_mapping.get(
                "short_signal", "PUT"
            ),
        }
    )
    drift = [
        field
        for field, expected in parameters.items()
        if actual.get(field) != expected
    ]
    if drift:
        raise ValueError(
            f"evidence binding {strategy_id!r} option-selection drift: "
            + ", ".join(sorted(drift))
        )
