from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    _compile_row,
    _validate_live_triage_canary,
    apply_canary_inhibition_overrides,
    apply_live_triage_provider_overlap_floor,
    compile_active_plan_from_rows,
    compute_live_triage_authorization_sha256,
)
from bhiksha.config.loader import load_strategy_catalog
from bhiksha.config.models import DeploymentManifest
from bhiksha.risk.canary_inhibition_store import CanaryInhibitionStore
from bhiksha.risk.demotion_store import DemotionStore


def _canary_metadata(
    *,
    overlap: float | None = 0.96,
    floor: float = 0.90,
    deployment_id: str = "pdd_live_canary",
) -> dict:
    packet_id = "a" * 64
    metadata = {
        "strategy_id": "triage-market_impulse-PDD__pdd_long",
        "run_id": "triage-w1w2-20260710-pdd-long",
        "evidence_packet_id": packet_id,
        "artifact_sha256": "b" * 64,
        "artifact_uri": (
            f"mala-evidence://sha256/{packet_id}/pdd_research_evidence.json"
        ),
        "canary_id": "pdd-v1",
        "canary_start_at": "2026-08-03T00:00:00-05:00",
        "canary_expires_at": "2026-08-28T15:15:00-05:00",
        "authorized_active_plan_id": "active_plan_2026-08-03",
        "authorized_deployment_id": deployment_id,
        "baseline_max_trade_premium_usd": 2000.0,
        "canary_policy": {
            "max_cumulative_loss_r": -2.0,
            "provider_overlap_floor": floor,
            "stop_on_unprotected_position": True,
            "stop_on_missing_attribution": True,
            "stop_on_failed_exit_receipt": True,
            "scale_min_clean_closes": 10,
            "r_definition": "sum_after_cost_trade_pnl_over_frozen_entry_stop_risk",
            "scale_fraction_of_baseline": 0.20,
            "round_trip_cost_per_contract_usd": 2.0,
        },
    }
    # Valid hex placeholder. Tests that compile a live row replace this with
    # a digest over the complete materialized deployment contract.
    metadata["authorization_sha256"] = "0" * 64
    if overlap is not None:
        metadata["provider_signal_overlap"] = overlap
    return metadata


def _deployment(
    deployment_id: str,
    *,
    triage: bool,
    shadow_only: bool = False,
    overlap: float | None = 0.96,
    floor: float = 0.90,
) -> DeploymentManifest:
    metadata = (
        _canary_metadata(overlap=overlap, floor=floor)
        if triage
        else {"strategy_id": "existing-live-strategy"}
    )
    return DeploymentManifest.model_validate(
        {
            "deployment_id": deployment_id,
            "enabled": True,
            "symbol": "PDD" if triage else "SPY",
            "strategy": {
                "key": "market_impulse",
                "version": 1,
                "params": {"direction": "long" if triage else "short"},
            },
            "execution": {
                "profile": "single_leg_long_premium_v1",
                "runtime_mode": "live_approval_gated",
                "shadow_only": shadow_only,
                "dte_min": 0,
                "dte_max": 3,
                "dte_fallback_policy": "allow_nearest_after",
            },
            "risk": {
                "profile": "conservative_day1",
                "max_trade_premium_usd": 300,
                "max_contracts": 1,
                "stop_loss_pct": 0.45,
            },
            "exit": {
                "profile": "strategy_managed_v1",
                "profile_exit_drives_live": True,
                "profile_exit_shadow_only": False,
                "stop_loss_pct": 0.45,
            },
            "source": {"origin": "active_sheet_strategy", "metadata": metadata},
        }
    )


def _write_catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "strategy_id": "triage-market_impulse-PDD__pdd_long",
                "enabled": True,
                "symbol": "PDD",
                "strategy": {
                    "key": "market_impulse",
                    "version": 1,
                    "params": {"direction": "long"},
                },
                "execution": {
                    "profile": "single_leg_long_premium_v1",
                    "shadow_only": True,
                    "dte_min": 0,
                    "dte_max": 3,
                    "dte_fallback_policy": "allow_nearest_after",
                },
                "risk": {
                    "profile": "conservative_day1",
                    "max_trade_premium_usd": 300,
                    "max_contracts": 1,
                    "stop_loss_pct": 0.45,
                },
                "exit": {
                    "profile": "strategy_managed_v1",
                    "profile_exit_drives_live": False,
                    "profile_exit_shadow_only": True,
                    "stop_loss_pct": 0.45,
                },
            }
        ),
        encoding="utf-8",
    )


def test_latched_live_triage_canary_compiles_shadow_with_warning(tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_catalog(catalog / "pdd.yaml")
    store = CanaryInhibitionStore(tmp_path / "canary_inhibitions.json")
    store.record_inhibition(
        deployment_id="pdd_live_canary",
        canary_id="pdd-v1",
        reason="provider_overlap_below_floor",
    )
    row = _authorized_live_row(catalog, overlap=0.89)

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog,
        trading_date="2026-08-03",
        risk_demotion_store=DemotionStore(tmp_path / "demotions.json"),
        canary_inhibition_store=store,
    )

    assert compiled.plan.suppressed == []
    assert compiled.plan.deployments[0].execution.shadow_only is True
    warning = compiled.plan.summary["canary_inhibition_warnings"][0]
    assert warning["deployment_id"] == "pdd_live_canary"
    assert warning["latched_canary_ids"] == ["pdd-v1"]
    assert warning["reason"] == "live_triage_canary_inhibited"
    assert compiled.plan.summary["live_triage_provider_overlap_warnings"][0][
        "reason"
    ] == "provider_signal_overlap_below_floor"


def test_unreadable_store_forces_only_live_triage_lanes_shadow(tmp_path) -> None:
    path = tmp_path / "canary_inhibitions.json"
    path.write_text("{broken", encoding="utf-8")
    deployments = [
        _deployment("pdd_live_canary", triage=True),
        _deployment("existing_live_lane", triage=False),
    ]

    updated, warnings = apply_canary_inhibition_overrides(
        deployments,
        inhibition_store=CanaryInhibitionStore(path),
    )

    by_id = {deployment.deployment_id: deployment for deployment in updated}
    assert by_id["pdd_live_canary"].execution.shadow_only is True
    assert by_id["existing_live_lane"].execution.shadow_only is False
    assert warnings[0]["reason"] == "canary_inhibition_state_unavailable"


@pytest.mark.parametrize(
    ("overlap", "reason"),
    [
        (0.89, "provider_signal_overlap_below_floor"),
        (None, "provider_signal_overlap_missing_or_invalid"),
    ],
)
def test_live_triage_provider_overlap_floor_is_checked_statically(
    overlap: float | None,
    reason: str,
) -> None:
    deployment = _deployment(
        "pdd_live_canary",
        triage=True,
        overlap=overlap,
    )

    updated, warnings = apply_live_triage_provider_overlap_floor([deployment])

    assert updated[0].execution.shadow_only is True
    assert warnings[0]["reason"] == reason


def test_live_triage_provider_overlap_policy_cannot_lower_static_floor() -> None:
    deployment = _deployment(
        "pdd_live_canary",
        triage=True,
        overlap=0.96,
        floor=0.89,
    )

    with pytest.raises(ValueError, match="provider_overlap_floor"):
        _validate_live_triage_canary(
            deployment,
            strategy_id="triage-market_impulse-PDD__pdd_long",
        )


def _live_row(
    deployment_id: str = "pdd_live_canary",
    *,
    metadata: dict | None = None,
    dte_max: int = 3,
) -> ActivePlanSheetRow:
    return ActivePlanSheetRow.model_validate(
        {
            "row_id": deployment_id,
            "row_type": "strategy",
            "strategy_id": "triage-market_impulse-PDD__pdd_long",
            "authorization_mode": "live",
            "max_trade_premium_usd": 300,
            "max_contracts": 1,
            "execution": {
                "runtime_mode": "live_approval_gated",
                "shadow_only": False,
                "dte_min": 0,
                "dte_max": dte_max,
                "dte_fallback_policy": "allow_nearest_after",
            },
            "exit": {
                "profile_exit_drives_live": True,
                "profile_exit_shadow_only": False,
            },
            "metadata": metadata
            if metadata is not None
            else _canary_metadata(deployment_id=deployment_id),
        }
    )


def _authorized_live_row(
    catalog: Path,
    deployment_id: str = "pdd_live_canary",
    *,
    dte_max: int = 3,
    overlap: float = 0.96,
) -> ActivePlanSheetRow:
    row = _live_row(
        deployment_id,
        dte_max=dte_max,
        metadata=_canary_metadata(
            deployment_id=deployment_id,
            overlap=overlap,
        ),
    )
    catalog_entries = load_strategy_catalog(catalog)
    deployment = _compile_row(
        row,
        {entry.strategy_id: entry for entry in catalog_entries},
    )
    metadata = dict(row.source_metadata)
    metadata["authorization_sha256"] = compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id=str(metadata["authorized_active_plan_id"]),
    )
    return row.model_copy(update={"source_metadata": metadata})


def test_live_triage_authorization_hash_is_tamper_evident(tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_catalog(catalog / "pdd.yaml")
    metadata = _canary_metadata()
    metadata["authorization_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="authorization_sha256"):
        compile_active_plan_from_rows(
            rows=[_live_row(metadata=metadata)],
            strategy_catalog_path=catalog,
            trading_date="2026-08-03",
        )


def test_live_triage_authorization_hash_binds_complete_deployment(tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_catalog(catalog / "pdd.yaml")
    authorized = _authorized_live_row(catalog)
    tampered = authorized.model_copy(
        update={"hard_flat_time_et": "15:00"}
    )

    with pytest.raises(ValueError, match="authorization_sha256"):
        compile_active_plan_from_rows(
            rows=[tampered],
            strategy_catalog_path=catalog,
            trading_date="2026-08-03",
        )


def test_live_triage_authority_rejects_dte_drift(tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_catalog(catalog / "pdd.yaml")

    with pytest.raises(ValueError, match="frozen 0-3 DTE"):
        compile_active_plan_from_rows(
            rows=[_authorized_live_row(catalog, dte_max=7)],
            strategy_catalog_path=catalog,
            trading_date="2026-08-03",
        )


def test_plan_cannot_authorize_two_live_triage_canaries(tmp_path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_catalog(catalog / "pdd.yaml")

    with pytest.raises(ValueError, match="only one live triage"):
        compile_active_plan_from_rows(
            rows=[
                _authorized_live_row(catalog, "pdd_live_one"),
                _authorized_live_row(catalog, "pdd_live_two"),
            ],
            strategy_catalog_path=catalog,
            trading_date="2026-08-03",
        )
