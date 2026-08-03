from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.config.models import DeploymentManifest
from bhiksha.evidence.bindings import (
    apply_evidence_binding,
    build_registry_payload,
    load_evidence_bindings,
)


def _deployment() -> DeploymentManifest:
    return DeploymentManifest.model_validate(
        {
            "deployment_id": "meta-shadow",
            "enabled": True,
            "symbol": "META",
            "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
            "execution": {
                "profile": "single_leg_long_premium_v1",
                "shadow_only": True,
                "dte_min": 0,
                "dte_max": 3,
                "dte_fallback_policy": "allow_nearest_after",
                "target_abs_delta_min": 0.15,
                "target_abs_delta_max": 0.35,
                "min_open_interest": 100,
                "max_bid_ask_spread_pct": 0.2,
                "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            },
            "risk": {"profile": "test", "max_trade_premium_usd": 500},
            "exit": {"profile": "test"},
            "source": {"origin": "test", "metadata": {"strategy_id": "market-meta"}},
        }
    )


def _binding() -> dict:
    option = {
        "schema_version": "mala.option_selection_contract.v1",
        "contract_id": "meta-option-v1",
        "selector_family": "single_leg_long_premium",
        "selector_implementation": "bhiksha.options.selectors.SingleLegOptionSelector",
        "selector_version": "1",
        "parameters": {
            "dte_min": 0,
            "dte_max": 3,
            "dte_fallback_policy": "allow_nearest_after",
            "target_abs_delta_min": 0.15,
            "target_abs_delta_max": 0.35,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.2,
            "long_signal_contract_type": "CALL",
            "short_signal_contract_type": "PUT",
        },
    }
    from bhiksha.evidence.bindings import canonical_sha256

    option["contract_sha256"] = canonical_sha256(option)
    return {
        "strategy_id": "market-meta",
        "symbol": "META",
        "direction": "short",
        "allowed_authorization_modes": ["shadow"],
        "run_id": "run-meta",
        "evidence_packet_id": "a" * 64,
        "artifact_sha256": "b" * 64,
        "artifact_uri": "mala-evidence://sha256/" + "a" * 64 + "/report.json",
        "experiment_id": "meta-experiment",
        "cohort_id": "meta-cohort",
        "cohort_contract_sha256": "c" * 64,
        "declared_option_selection_contract": option,
    }


def test_binding_is_observational_and_exact() -> None:
    registry = build_registry_payload([_binding()])
    binding = registry["bindings"][0]
    original = _deployment()
    bound = apply_evidence_binding(
        original,
        strategy_id="market-meta",
        authorization_mode="shadow",
        bindings={"market-meta": binding},
    )
    assert bound.execution == original.execution
    assert bound.risk == original.risk
    assert bound.exit == original.exit
    assert bound.source.metadata["evidence_packet_id"] == "a" * 64
    assert bound.source.metadata["authorization_identity_status"] == "shadow_observation"
    assert len(bound.source.metadata["deployment_contract_sha256"]) == 64


def test_binding_rejects_selector_drift() -> None:
    registry = build_registry_payload([_binding()])
    deployment = _deployment().model_copy(
        update={
            "execution": _deployment().execution.model_copy(update={"dte_max": 7})
        }
    )
    with pytest.raises(ValueError, match="option-selection drift: dte_max"):
        apply_evidence_binding(
            deployment,
            strategy_id="market-meta",
            authorization_mode="shadow",
            bindings={"market-meta": registry["bindings"][0]},
        )


def test_registry_digest_fails_closed(tmp_path: Path) -> None:
    payload = build_registry_payload([_binding()])
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert "market-meta" in load_evidence_bindings(path)
    payload["bindings"][0]["symbol"] = "NVDA"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="registry digest mismatch"):
        load_evidence_bindings(path)


def test_authorized_historical_packet_is_preserved_for_v2_observation_binding() -> None:
    registry = build_registry_payload([_binding()])
    deployment = _deployment()
    metadata = {
        **deployment.source.metadata,
        "authorization_sha256": "f" * 64,
        "evidence_packet_id": "1" * 64,
        "artifact_sha256": "2" * 64,
        "artifact_uri": "mala-evidence://sha256/" + "1" * 64 + "/old.json",
    }
    deployment = deployment.model_copy(
        update={
            "source": deployment.source.model_copy(
                update={"metadata": metadata}
            )
        }
    )

    updated = apply_evidence_binding(
        deployment,
        strategy_id="market-meta",
        authorization_mode="shadow",
        bindings={"market-meta": registry["bindings"][0]},
    )

    result = updated.source.metadata
    assert result["evidence_packet_id"] == "1" * 64
    assert result["artifact_sha256"] == "2" * 64
    assert result["observation_evidence_packet_id"] == "a" * 64
    assert result["observation_evidence_artifact_sha256"] == "b" * 64
    assert result["authorization_identity_status"] == "compiled_observation_only"
