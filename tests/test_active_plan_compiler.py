from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from bhiksha.active_plan.compiler import (
    ActivePlanSheetRow,
    StrategyCatalogSheetRow,
    _compile_row,
    compile_active_plan_from_google_sheets,
    compile_active_plan_from_rows,
    compile_active_plan_from_sheet,
    compute_live_triage_authorization_sha256,
    sync_google_strategy_catalog,
)
from bhiksha.config.loader import load_active_plan, load_strategy_catalog
from bhiksha.config.models import ActivePlan, DeploymentManifest
from bhiksha.execution.exit_policy import canonical_policy_hash
from bhiksha.evidence.bindings import build_registry_payload, canonical_sha256
from bhiksha.tools.compile_active_plan import main as compile_active_plan_main


def test_compile_active_plan_from_csv_supports_strategy_and_manual_same_symbol(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_jerk.yaml", strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_jerk_live_today",
                "row_type": "strategy",
                "strategy_id": "spy_jerk_pivot_short_v1",
                "authorization_mode": "live",
                "max_trade_premium_usd": "200",
                "entry_window_start_et": "09:40",
                "notes": "primary opening lane",
                "execution_overrides": json.dumps(
                    {
                        "dte_min": 1,
                        "dte_max": 5,
                        "entry_execution_profile": "patient",
                        "entry_pricing_spread_fraction": 0.25,
                        "entry_pricing_oi_percentile_scale": True,
                        "entry_reprice_enabled": True,
                        "entry_reprice_checkpoints_seconds": [60, 180],
                        "entry_reprice_cancel_after_seconds": 300,
                        "entry_reprice_spread_fractions": [0.50, 0.70],
                    }
                ),
            },
            {
                "row_id": "spy_breakout_manual",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                "after_time_et": "09:35",
                "profit_target_multiple": "2.0",
                "stop_loss_pct": "0.35",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-09",
        trading_date="2026-04-09",
        source_name="test_sheet",
    )

    assert compiled.plan.active_plan_id == "active_plan_2026-04-09"
    assert compiled.plan.summary["deployment_count"] == 2
    assert compiled.plan.summary["symbols"] == ["SPY"]
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "spy_jerk_live_today",
        "spy_breakout_manual",
    ]

    strategy = compiled.plan.deployments[0]
    assert strategy.execution.shadow_only is False
    assert strategy.execution.dte_min == 1
    assert strategy.execution.dte_max == 5
    assert strategy.execution.entry_execution_profile == "patient"
    assert strategy.execution.entry_pricing_spread_fraction == 0.25
    assert strategy.execution.entry_pricing_oi_percentile_scale is True
    assert strategy.execution.entry_reprice_enabled is True
    assert strategy.execution.entry_reprice_checkpoints_seconds == [60, 180]
    assert strategy.execution.entry_reprice_cancel_after_seconds == 300
    assert strategy.execution.entry_reprice_spread_fractions == [0.50, 0.70]
    assert strategy.execution.entry_reprice_max_chase_pct == 0.10
    assert strategy.execution.entry_window_start_et == "09:40"
    assert strategy.risk.max_trade_premium_usd == 200
    assert strategy.source.origin == "active_sheet_strategy"
    assert strategy.source.metadata["strategy_id"] == "spy_jerk_pivot_short_v1"

    manual = compiled.plan.deployments[1]
    assert manual.strategy.key == "manual_trigger"
    assert manual.execution.shadow_only is True
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.exit.use_profit_target is True
    assert manual.exit.profit_target_multiple == 2.0
    assert manual.exit.stop_loss_pct == 0.35
    assert manual.source.metadata["manual_setup_type"] == "manual_trigger"


def _live_triage_canary_row(*, metadata: dict | None = None) -> ActivePlanSheetRow:
    packet_id = "a" * 64
    default_metadata = {
        "run_id": "triage-w1w2-20260710-pdd-long",
        "evidence_packet_id": packet_id,
        "artifact_sha256": "b" * 64,
        "artifact_uri": f"mala-evidence://sha256/{packet_id}/pdd_research_evidence.json",
        "canary_id": "pdd-live-canary-v1",
        "canary_start_at": "2026-08-03T00:00:00-05:00",
        "canary_expires_at": "2026-08-28T15:15:00-05:00",
        "authorized_active_plan_id": "active_plan_2026-08-03",
        "authorized_deployment_id": "pdd_live_canary",
        "baseline_max_trade_premium_usd": 2000.0,
        "provider_signal_overlap": 0.96,
        "canary_policy": {
            "max_cumulative_loss_r": -2.0,
            "provider_overlap_floor": 0.90,
            "stop_on_unprotected_position": True,
            "stop_on_missing_attribution": True,
            "stop_on_failed_exit_receipt": True,
            "scale_min_clean_closes": 10,
            "r_definition": "sum_after_cost_trade_pnl_over_frozen_entry_stop_risk",
            "scale_fraction_of_baseline": 0.20,
            "round_trip_cost_per_contract_usd": 2.0,
        },
    }
    default_metadata["authorization_sha256"] = "0" * 64
    return ActivePlanSheetRow.model_validate(
        {
            "row_id": "pdd_live_canary",
            "row_type": "strategy",
            "strategy_id": "triage-market_impulse-PDD__pdd_long",
            "authorization_mode": "live",
            "max_trade_premium_usd": 300,
            "max_contracts": 1,
            "execution": {
                "runtime_mode": "live_approval_gated",
                "shadow_only": False,
                "dte_min": 0,
                "dte_max": 3,
                "dte_fallback_policy": "allow_nearest_after",
            },
            "exit": {"profile_exit_drives_live": True, "profile_exit_shadow_only": False},
            "metadata": default_metadata if metadata is None else metadata,
        }
    )


def _authorized_live_triage_canary_row(
    catalog_root: Path,
) -> ActivePlanSheetRow:
    row = _live_triage_canary_row()
    catalog = load_strategy_catalog(catalog_root)
    deployment = _compile_row(
        row,
        {entry.strategy_id: entry for entry in catalog},
    )
    metadata = dict(row.source_metadata)
    metadata["authorization_sha256"] = compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id="active_plan_2026-08-03",
    )
    return row.model_copy(update={"source_metadata": metadata})


def _authorized_v2_live_triage_canary_row(
    catalog_root: Path,
) -> ActivePlanSheetRow:
    row = _live_triage_canary_row()
    metadata = dict(row.source_metadata)
    metadata["authorization_contract_version"] = "pdd-entry-canary.v2"
    metadata["canary_id"] = "pdd-live-canary-v2"
    policy = dict(metadata["canary_policy"])
    policy["scale_fraction_of_baseline"] = 0.50
    metadata["canary_policy"] = policy
    row = row.model_copy(
        update={
            "max_trade_premium_usd": 1_000.0,
            "max_contracts": 2,
            "source_metadata": metadata,
        }
    )
    catalog = load_strategy_catalog(catalog_root)
    deployment = _compile_row(
        row,
        {entry.strategy_id: entry for entry in catalog},
    )
    metadata = dict(row.source_metadata)
    metadata["authorization_sha256"] = compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id="active_plan_2026-08-03",
    )
    return row.model_copy(update={"source_metadata": metadata})


def _authorized_continuing_live_triage_row(
    catalog_root: Path,
) -> ActivePlanSheetRow:
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    metadata = dict(row.source_metadata)
    metadata.update(
        {
            "authorization_contract_version": "pdd-entry-live.v1",
            "experiment_status": "closed",
            "continuing_live_authorized_by": "Suman",
            "continuing_live_authorized_at": "2026-08-31T09:05:00-05:00",
        }
    )
    metadata["authorization_sha256"] = "0" * 64
    row = row.model_copy(update={"source_metadata": metadata})
    catalog = load_strategy_catalog(catalog_root)
    deployment = _compile_row(
        row,
        {entry.strategy_id: entry for entry in catalog},
    )
    metadata["authorization_sha256"] = compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id="active_plan_2026-08-03",
    )
    return row.model_copy(update={"source_metadata": metadata})


def test_live_triage_canary_requires_immutable_identity_and_bounded_policy(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )

    compiled = compile_active_plan_from_rows(
        rows=[_live_triage_canary_row(metadata={})],
        strategy_catalog_path=catalog_root,
        trading_date="2026-08-03",
    )

    assert compiled.plan.deployments == []
    assert "missing identity metadata" in compiled.plan.suppressed[0]["reason"]


def test_live_triage_canary_carries_packet_and_stop_gates_into_plan(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )

    compiled = compile_active_plan_from_rows(
        rows=[_authorized_live_triage_canary_row(catalog_root)],
        strategy_catalog_path=catalog_root,
        trading_date="2026-08-03",
    )

    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.shadow_only is False
    assert deployment.risk.max_contracts == 1
    assert deployment.risk.max_trade_premium_usd == 300
    assert deployment.source.metadata["evidence_packet_id"] == "a" * 64
    assert deployment.source.metadata["canary_policy"]["scale_min_clean_closes"] == 10


def test_v2_live_triage_canary_authorizes_two_contract_1000_cap(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )

    compiled = compile_active_plan_from_rows(
        rows=[_authorized_v2_live_triage_canary_row(catalog_root)],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    )

    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.risk.max_contracts == 2
    assert deployment.risk.max_trade_premium_usd == 1_000.0
    assert (
        deployment.source.metadata["authorization_contract_version"]
        == "pdd-entry-canary.v2"
    )
    assert (
        deployment.source.metadata["canary_policy"][
            "scale_fraction_of_baseline"
        ]
        == 0.50
    )


def test_continuing_live_triage_closes_canary_and_compiles_after_expiry(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )

    compiled = compile_active_plan_from_rows(
        rows=[_authorized_continuing_live_triage_row(catalog_root)],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-31",
        trading_date="2026-08-31",
    )

    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.shadow_only is False
    assert deployment.risk.max_contracts == 2
    assert deployment.risk.max_trade_premium_usd == 1_000.0
    assert deployment.source.metadata["experiment_status"] == "closed"
    assert (
        deployment.source.metadata["authorization_contract_version"]
        == "pdd-entry-live.v1"
    )


def test_continuing_live_triage_requires_explicit_closed_experiment(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_continuing_live_triage_row(catalog_root)
    metadata = dict(row.source_metadata)
    metadata["experiment_status"] = "active"
    row = row.model_copy(update={"source_metadata": metadata})

    with pytest.raises(ValueError, match="experiment_status=closed"):
        compile_active_plan_from_rows(
            rows=[row],
            strategy_catalog_path=catalog_root,
            active_plan_id="active_plan_2026-08-03",
            trading_date="2026-08-31",
        )


def test_v2_live_triage_canary_rejects_three_contracts(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    row = row.model_copy(update={"max_contracts": 3})

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    )

    assert compiled.plan.deployments == []
    assert "requires max_contracts=2" in compiled.plan.suppressed[0]["reason"]


@pytest.mark.parametrize(
    ("baseline_cap", "canary_cap", "expected_reason"),
    [
        (4_000.0, 1_000.0, "baseline_max_trade_premium_usd=2000"),
        (1_000.0, 500.0, "baseline_max_trade_premium_usd=2000"),
        (2_000.0, 999.0, "max_trade_premium_usd=1000"),
    ],
)
def test_v2_live_triage_canary_requires_exact_operator_authorized_size(
    tmp_path: Path,
    baseline_cap: float,
    canary_cap: float,
    expected_reason: str,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    metadata = dict(row.source_metadata)
    metadata["baseline_max_trade_premium_usd"] = baseline_cap
    row = row.model_copy(
        update={
            "max_trade_premium_usd": canary_cap,
            "source_metadata": metadata,
        }
    )
    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    )

    assert compiled.plan.deployments == []
    assert expected_reason in compiled.plan.suppressed[0]["reason"]


@pytest.mark.parametrize(
    ("authorization_contract_version", "expected_reason"),
    [
        ("pdd-entry-canary.v3", "unsupported authorization_contract_version"),
        (None, "requires max_contracts=1"),
    ],
)
def test_v2_live_triage_canary_rejects_unknown_or_downgraded_contract(
    tmp_path: Path,
    authorization_contract_version: str | None,
    expected_reason: str,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    metadata = dict(row.source_metadata)
    if authorization_contract_version is None:
        metadata.pop("authorization_contract_version", None)
    else:
        metadata["authorization_contract_version"] = (
            authorization_contract_version
        )
    row = row.model_copy(update={"source_metadata": metadata})
    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    )

    assert compiled.plan.deployments == []
    assert expected_reason in compiled.plan.suppressed[0]["reason"]


def test_v2_live_triage_authorization_hash_binds_size_fields(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    catalog = load_strategy_catalog(catalog_root)
    deployment = _compile_row(
        row,
        {entry.strategy_id: entry for entry in catalog},
    )
    tampered = deployment.model_copy(
        update={
            "risk": deployment.risk.model_copy(
                update={"max_trade_premium_usd": 999.0}
            )
        }
    )

    assert compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id="active_plan_2026-08-03",
    ) != compute_live_triage_authorization_sha256(
        tampered,
        active_plan_id="active_plan_2026-08-03",
    )


def test_retained_pdd_release_candidate_recomputes_exact_authorization() -> None:
    receipt_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "release_candidates"
        / "mala_research_release_20260802"
        / "pdd_canary_candidate.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    deployment = DeploymentManifest.model_validate(
        receipt["authorization_payload"]["deployment"]
    )

    assert compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id=receipt["authorization_payload"]["active_plan_id"],
    ) == receipt["authorization_sha256"]
    assert receipt["authorization_sha256"] == (
        "7500d4f18bd7f0dde4697d4a77efcb90f881365f51fd898b33823a4f644efa01"
    )
    assert receipt["plan_revision_id"] == (
        "sha256:608a2641d7b70d8c038ca3c866c6bb153b5d274518c75c99e1241f7272530e56"
    )
    active_plan_bytes = gzip.decompress(
        base64.b64decode(receipt["active_plan_gzip_base64"])
    )
    assert hashlib.sha256(active_plan_bytes).hexdigest() == receipt["active_plan_sha256"]
    plan_payload = json.loads(active_plan_bytes)
    claimed_revision = plan_payload.pop("plan_revision_id")
    assert ActivePlan.model_validate(plan_payload).plan_revision_id == claimed_revision
    assert claimed_revision == receipt["plan_revision_id"]
    assert receipt["projection_assertions"]["qqq_triage_present"] is False
    assert receipt["projection_assertions"]["iwm_risk_envelope_mode"] == "off"


def test_retained_pdd_v2_release_candidate_recomputes_exact_authorization() -> None:
    receipt_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "release_candidates"
        / "pdd_resize_20260802"
        / "pdd_canary_candidate_v2.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    deployment = DeploymentManifest.model_validate(
        receipt["authorization_payload"]["deployment"]
    )

    assert compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id=receipt["authorization_payload"]["active_plan_id"],
    ) == receipt["authorization_sha256"]
    assert receipt["authorization_sha256"] == (
        "4d8bb1d188190ee5d0e2c066fc960f99daa79b01a34dcefa9ff11cd4b9663539"
    )
    assert receipt["plan_revision_id"] == (
        "sha256:fb7fa031bbc27b532ee99e6aa04470c540995c91483bac40958826a53cfab510"
    )
    active_plan_bytes = gzip.decompress(
        base64.b64decode(receipt["active_plan_gzip_base64"])
    )
    assert hashlib.sha256(active_plan_bytes).hexdigest() == (
        "30c498b6ad3c6b9ce97a131edb8e540f66071008910c402168e99679d65f871d"
    )
    plan_payload = json.loads(active_plan_bytes)
    claimed_revision = plan_payload.pop("plan_revision_id")
    assert ActivePlan.model_validate(plan_payload).plan_revision_id == claimed_revision
    assertions = receipt["projection_assertions"]
    assert assertions["pdd_max_trade_premium_usd"] == 1_000.0
    assert assertions["pdd_max_contracts"] == 2
    assert assertions["pdd_profile_exit_id"] == "profile__trend_continuation"
    assert assertions["pdd_target_1_quantity"] == 0.60
    assert assertions["pdd_target_1_contracts"] == 1
    assert assertions["pdd_runner_contracts"] == 1
    assert assertions["pdd_risk_envelope_live_mode"] == "off"


def test_pdd_v2_authorization_validates_before_observation_binding(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _authorized_v2_live_triage_canary_row(catalog_root)
    baseline = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    ).plan.deployments[0]
    option = {
        "schema_version": "mala.option_selection_contract.v1",
        "contract_id": "pdd-observation-v2",
        "selector_family": "single_leg_long_premium",
        "selector_implementation": "bhiksha.options.selectors.SingleLegOptionSelector",
        "selector_version": "1",
        "parameters": {
            "dte_min": baseline.execution.dte_min,
            "dte_max": baseline.execution.dte_max,
            "dte_fallback_policy": baseline.execution.dte_fallback_policy,
            "target_abs_delta_min": baseline.execution.target_abs_delta_min,
            "target_abs_delta_max": baseline.execution.target_abs_delta_max,
            "min_open_interest": baseline.execution.min_open_interest,
            "max_bid_ask_spread_pct": baseline.execution.max_bid_ask_spread_pct,
            "long_signal_contract_type": baseline.execution.option_mapping["long_signal"],
            "short_signal_contract_type": baseline.execution.option_mapping["short_signal"],
        },
    }
    option["contract_sha256"] = canonical_sha256(option)
    registry = build_registry_payload(
        [
            {
                "strategy_id": "triage-market_impulse-PDD__pdd_long",
                "symbol": "PDD",
                "direction": baseline.strategy.params["direction"],
                "allowed_authorization_modes": ["live"],
                "run_id": "triage-w1w2-20260710-pdd-long",
                "evidence_packet_id": "9" * 64,
                "artifact_sha256": "8" * 64,
                "artifact_uri": "mala-evidence://sha256/" + "9" * 64 + "/observation.json",
                "experiment_id": "pdd-prospective-v2",
                "cohort_id": "pdd-next20-v2",
                "cohort_contract_sha256": "7" * 64,
                "declared_option_selection_contract": option,
            }
        ]
    )

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
        evidence_bindings={
            "triage-market_impulse-PDD__pdd_long": registry["bindings"][0]
        },
    )

    assert len(compiled.plan.deployments) == 1
    deployment = compiled.plan.deployments[0]
    assert deployment.risk.max_trade_premium_usd == 1_000.0
    assert deployment.risk.max_contracts == 2
    assert deployment.source.metadata["evidence_packet_id"] == "a" * 64
    assert deployment.source.metadata["observation_evidence_packet_id"] == "9" * 64
    assert deployment.source.metadata["authorization_identity_status"] == "compiled_observation_only"


@pytest.mark.parametrize(
    ("baseline_cap", "canary_cap", "expected_reason"),
    [
        (2_000.0, 299.0, "premium cap must equal min(300, 20%"),
        (2_000.0, 301.0, "requires max_trade_premium_usd<=300"),
        (1_000.0, 300.0, "premium cap must equal min(300, 20%"),
    ],
)
def test_live_triage_canary_requires_exact_lower_of_300_and_20_percent_baseline(
    tmp_path: Path,
    baseline_cap: float,
    canary_cap: float,
    expected_reason: str,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "pdd.yaml",
        strategy_id="triage-market_impulse-PDD__pdd_long",
        symbol="PDD",
    )
    row = _live_triage_canary_row()
    metadata = dict(row.source_metadata)
    metadata["baseline_max_trade_premium_usd"] = baseline_cap
    row = row.model_copy(
        update={
            "max_trade_premium_usd": canary_cap,
            "source_metadata": metadata,
        }
    )

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-08-03",
        trading_date="2026-08-03",
    )

    assert compiled.plan.deployments == []
    assert expected_reason in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_unknown_strategy_id(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    sheet_path = tmp_path / "sheet.json"
    sheet_path.write_text(
        json.dumps(
            [
                {
                    "row_id": "unknown_strategy_lane",
                    "row_type": "strategy",
                    "strategy_id": "missing_strategy",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-09",
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "Unknown strategy_id" in compiled.plan.suppressed[0]["reason"]
    coverage = compiled.plan.summary["coverage"]
    assert coverage["intentional_pre_observation_suppression_count"] == 0
    assert coverage["unexpected_coverage_loss_count"] == 1
    assert coverage["unexpected_coverage_loss_deployment_ids"] == [
        "unknown_strategy_lane"
    ]
    assert coverage["release_safe"] is False


def test_compile_active_plan_cli_writes_active_plan_json(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "qqq_impulse.yaml", strategy_id="qqq_market_impulse_short_v1", symbol="QQQ")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "qqq_impulse_shadow",
                "row_type": "strategy",
                "strategy_id": "qqq_market_impulse_short_v1",
                "authorization_mode": "shadow",
            }
        ],
    )
    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"

    exit_code = compile_active_plan_main(
        [
            "--sheet",
            str(sheet_path),
            "--strategy-catalog",
            str(catalog_root),
            "--out",
            str(output_path),
            "--active-plan-id",
            "active_plan_2026-04-09",
            "--trading-date",
            "2026-04-09",
        ]
    )

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-09"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["qqq_impulse_shadow"]


def test_compile_active_plan_accepts_operator_friendly_alias_columns(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "live",
                "strategy": "market_impulse_spy_short_v1",
                "max_premium": "180",
                "start": "09:40",
                "end": "10:30",
            },
            {
                "id": "spy_breakout_lane",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "spy",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "09:35",
                "target_r": "2.5",
                "stop_pct": "0.30",
                "flat_time": "15:50",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-10",
        trading_date="2026-04-10",
    )

    strategy = compiled.plan.deployments[0]
    assert strategy.deployment_id == "spy_strategy_lane"
    assert strategy.execution.shadow_only is False
    assert strategy.execution.entry_window_start_et == "09:40"
    assert strategy.execution.entry_window_end_et == "10:30"
    assert strategy.risk.max_trade_premium_usd == 180

    manual = compiled.plan.deployments[1]
    assert manual.deployment_id == "spy_breakout_lane"
    assert manual.symbol == "SPY"
    assert manual.execution.shadow_only is True
    assert manual.strategy.key == "manual_breakout"
    assert manual.strategy.params["trigger_price"] == 603.25
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.execution.dte_max == 5
    assert manual.execution.target_abs_delta_min == 0.30
    assert manual.execution.target_abs_delta_max == 0.70
    assert manual.execution.min_open_interest == 50
    assert manual.execution.max_bid_ask_spread_pct == 0.25
    assert manual.exit.stop_loss_pct == 0.30
    assert manual.exit.profit_target_multiple == 2.5
    assert manual.exit.hard_flat_time_et == "15:50"
    assert manual.source.metadata["manual_setup_type"] == "breakout"


def test_compile_active_plan_normalizes_loose_sheet_times(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
                "start": "9:30",
                "end": "10:05",
            },
            {
                "id": "spy_breakout_lane",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "9:35",
                "flat_time": "15:05",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-10",
        trading_date="2026-04-10",
    )

    strategy = compiled.plan.deployments[0]
    manual = compiled.plan.deployments[1]
    assert strategy.execution.entry_window_start_et == "09:30"
    assert strategy.execution.entry_window_end_et == "10:05"
    assert manual.strategy.key == "manual_breakout"
    assert manual.strategy.params["after_time_et"] == "09:35"
    assert manual.exit.hard_flat_time_et == "15:05"


def test_compile_active_plan_suppresses_invalid_rows_but_keeps_valid_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
            },
            {
                "id": "bad_manual_lane",
                "type": "manual",
                "setup": "reversion",
                "mode": "shadow",
                "symbol": "TSLA",
                "direction": "short",
                "trigger": "250",
                "trigger_when": "BELOW",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-10",
    )

    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == ["spy_strategy_lane"]
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "bad_manual_lane"
    assert compiled.plan.suppressed[0]["sheet_name"] == "sheet.csv"
    assert "manual_setup_type=manual_trigger or breakout" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_manual_row_with_invalid_after_time(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "id": "spy_strategy_lane",
                "type": "strategy",
                "mode": "shadow",
                "strategy": "market_impulse_spy_short_v1",
            },
            {
                "id": "bad_manual_after",
                "type": "manual",
                "setup": "breakout",
                "mode": "shadow",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "bad-time",
            },
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-04-10",
    )

    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == ["spy_strategy_lane"]
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "bad_manual_after"
    assert "Invalid time value" in compiled.plan.suppressed[0]["reason"]


# --- exit-profile config bridge -------------------------------------------------
#
# A Sheet/row can carry an assigned exit profile as a single JSON cell holding
# the kernel ``ManagementPolicySpec`` (model_dump). Column name:
# ``management_policy_spec`` (alias ``exit_profile_spec``). The compiler maps its
# v2 fields onto the compiled ``DeploymentManifest.exit`` (ExitSpec) so the live
# profile-exit evaluator sees the operator's exit DNA. Below proves: (a) every
# mapped field lands with the right value incl. the giveback enum, (b) bad bounds
# / bad enum are rejected (row suppressed, not crashed), (c) a row WITHOUT a
# profile compiles to the default ExitSpec for that path (back-compat).


def _exit_profile_spec(**overrides) -> dict[str, object]:
    """A representative kernel ManagementPolicySpec payload (staged-R scalp)."""
    payload: dict[str, object] = {
        "policy_id": "opening_drive_scalp_v1",
        "stop_family": "premium_pct",
        "stop_anchor": "option_premium",
        "exit_family": "staged_r",
        "target_model": "staged_r",
        "target_r": 2.0,
        "target_1_r": 1.0,
        "target_2_r": 2.0,
        "target_1_quantity": 0.5,
        "initial_stop_pct": 0.30,
        "premium_disaster_stop_pct": 0.50,
        "no_progress_seconds": 600,
        "max_hold_seconds": 5400,
        "high_water_giveback_policy": "MODERATE",
        "breakeven_after_t1": False,
        "eod_flat": False,
        "hard_flat_time_et": "15:50",
        "option_stop_fallback_pct": 0.40,
    }
    payload.update(overrides)
    return payload


def test_compile_active_plan_carries_exit_profile_spec_onto_manual_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_manual_profile",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                "management_policy_spec": json.dumps(_exit_profile_spec()),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.suppressed == []
    assert len(compiled.plan.deployments) == 1
    exit_spec = compiled.plan.deployments[0].exit

    # Every mapped v2 field carried with the right value.
    assert exit_spec.profile_exit_id == "opening_drive_scalp_v1"
    assert exit_spec.target_1_r == 1.0
    assert exit_spec.target_2_r == 2.0
    assert exit_spec.target_1_quantity == 0.5
    assert exit_spec.initial_stop_pct == 0.30
    assert exit_spec.premium_disaster_stop_pct == 0.50
    assert exit_spec.no_progress_seconds == 600
    assert exit_spec.max_hold_seconds == 5400
    # giveback enum carried verbatim (validated by the kernel spec).
    assert exit_spec.high_water_giveback_policy == "MODERATE"
    assert exit_spec.giveback_arm_r == 1.25
    assert exit_spec.giveback_retrace_fraction == 0.50
    assert exit_spec.exit_policy_schema_version == "exit-policy.v1"
    assert exit_spec.exit_policy_id == "opening_drive_scalp_v1.bhiksha.compat.v1"
    assert len(exit_spec.exit_policy_hash or "") == 64
    assert exit_spec.exit_policy_provenance["resolution"] == (
        "bhiksha_legacy_compatibility_map"
    )
    assert exit_spec.exit_policy_snapshot["giveback_arm_r"] == 1.25
    assert exit_spec.exit_policy_snapshot["parameters"][
        "no_progress_favorable_floor_r"
    ] == 0.25
    assert "profit_target_multiple" in exit_spec.exit_policy_snapshot["parameters"]
    assert exit_spec.breakeven_after_t1 is False
    assert exit_spec.eod_flat is False
    # option_stop_fallback_pct -> exit.stop_loss_pct (the resolvable recovery stop)
    # and hard_flat_time_et carried + normalized.
    assert exit_spec.stop_loss_pct == 0.40
    assert exit_spec.hard_flat_time_et == "15:50"

    # HARD BOUNDARY: the bridge never flips live enablement. drives_live stays
    # default False (record-only / shadow); the legacy shadow flag stays True.
    assert exit_spec.profile_exit_drives_live is False
    assert exit_spec.profile_exit_shadow_only is True


def test_compile_active_plan_carries_exit_profile_spec_onto_strategy_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_jerk.yaml", strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_jerk_profile_lane",
                "row_type": "strategy",
                "strategy_id": "spy_jerk_pivot_short_v1",
                "authorization_mode": "shadow",
                # alias column name also resolves to exit_profile_spec
                "exit_profile_spec": json.dumps(
                    _exit_profile_spec(policy_id="jerk_scalp_v1", high_water_giveback_policy="STRICT")
                ),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.suppressed == []
    exit_spec = compiled.plan.deployments[0].exit
    assert exit_spec.profile_exit_id == "jerk_scalp_v1"
    assert exit_spec.high_water_giveback_policy == "STRICT"
    assert exit_spec.target_1_quantity == 0.5
    assert exit_spec.initial_stop_pct == 0.30
    assert exit_spec.profile_exit_drives_live is False


def test_exit_profile_fallback_does_not_widen_existing_catalog_stop(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    catalog_path = catalog_root / "spy_jerk.yaml"
    _write_catalog_entry(catalog_path, strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")
    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    payload["risk"]["stop_loss_pct"] = 0.35
    payload["exit"]["stop_loss_pct"] = 0.35
    catalog_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_jerk_profile_lane",
                "row_type": "strategy",
                "strategy_id": "spy_jerk_pivot_short_v1",
                "authorization_mode": "shadow",
                "exit_profile_spec": json.dumps(_exit_profile_spec(option_stop_fallback_pct=0.40)),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.suppressed == []
    deployment = compiled.plan.deployments[0]
    assert deployment.risk.stop_loss_pct == 0.35
    assert deployment.exit.stop_loss_pct == 0.35
    assert deployment.exit.exit_policy_snapshot["option_stop_fallback_pct"] == 0.35
    assert deployment.exit.exit_policy_hash == canonical_policy_hash(
        deployment.exit.exit_policy_snapshot
    )
    assert deployment.exit.exit_policy_provenance["effective_override_keys"] == [
        "stop_loss_pct"
    ]
    assert deployment.exit.initial_stop_pct == 0.30
    assert deployment.exit.profile_exit_id == "opening_drive_scalp_v1"


def test_compile_active_plan_carries_mala_evidence_exit_profile_spec_onto_strategy_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_jerk.yaml", strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")

    compiled = compile_active_plan_from_rows(
        rows=[
            ActivePlanSheetRow.model_validate(
                {
                    "row_id": "spy_jerk_profile_lane",
                    "row_type": "strategy",
                    "strategy_id": "spy_jerk_pivot_short_v1",
                    "authorization_mode": "shadow",
                }
            )
        ],
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
        google_strategy_catalog=[
            StrategyCatalogSheetRow.model_validate(
                {
                    "catalog_key": "spy_jerk_pivot_short_v1",
                    "symbol": "SPY",
                    "strategy_key": "jerk_pivot_momentum",
                    "strategy_name": "Jerk-Pivot Momentum (tight)",
                    "bhiksha_ready": True,
                    "mala_evidence_ready": True,
                    "activation_candidate": True,
                    "option_trade_ready": True,
                    "management_policy_spec": json.dumps(
                        _exit_profile_spec(
                            policy_id="mala_evidence_profile_v1",
                            high_water_giveback_policy="STRICT",
                        )
                    ),
                }
            )
        ],
    )

    assert compiled.plan.suppressed == []
    exit_spec = compiled.plan.deployments[0].exit
    assert exit_spec.profile_exit_id == "mala_evidence_profile_v1"
    assert exit_spec.high_water_giveback_policy == "STRICT"
    assert exit_spec.target_1_r == 1.0
    assert exit_spec.initial_stop_pct == 0.30
    assert exit_spec.profile_exit_drives_live is False
    assert (
        compiled.plan.deployments[0].source.metadata["exit_profile_spec"]["policy_id"]
        == "mala_evidence_profile_v1"
    )


def test_exit_profile_spec_does_not_clobber_operator_dedicated_columns(tmp_path: Path) -> None:
    """The operator's dedicated typed columns win over the published spec.

    Regression: the spec's stop_loss_pct / hard_flat_time_et (which the kernel
    ManagementPolicySpec always emits, defaulting them non-None) must NOT
    overwrite the values the operator typed into the dedicated Sheet columns.
    Non-conflicting spec fields still carry.
    """
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_manual_profile_plus_columns",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                # spec carries option_stop_fallback_pct=0.40, hard_flat_time_et="15:50"
                "management_policy_spec": json.dumps(_exit_profile_spec()),
                # operator's dedicated typed columns differ from the spec values
                "stop_loss_pct": "0.60",
                "hard_flat_time_et": "15:40",
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.suppressed == []
    exit_spec = compiled.plan.deployments[0].exit
    # The operator's dedicated columns win over the spec (not 0.40 / "15:50").
    assert exit_spec.stop_loss_pct == 0.60
    assert exit_spec.hard_flat_time_et == "15:40"
    # Non-conflicting spec fields still carry.
    assert exit_spec.profile_exit_id == "opening_drive_scalp_v1"
    assert exit_spec.target_1_r == 1.0
    assert exit_spec.initial_stop_pct == 0.30
    assert exit_spec.profile_exit_drives_live is False


def test_compile_active_plan_suppresses_exit_profile_spec_with_out_of_bounds_quantity(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_manual_bad_qty",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                # target_1_quantity must be in [0, 1]; 1.5 is rejected by the
                # kernel ManagementPolicySpec validator.
                "management_policy_spec": json.dumps(_exit_profile_spec(target_1_quantity=1.5)),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "spy_manual_bad_qty"
    assert "target_1_quantity" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_exit_profile_spec_with_bad_giveback_enum(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_manual_bad_giveback",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                "management_policy_spec": json.dumps(
                    _exit_profile_spec(high_water_giveback_policy="AGGRESSIVE")
                ),
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert compiled.plan.suppressed[0]["row_id"] == "spy_manual_bad_giveback"
    assert "high_water_giveback_policy" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_without_exit_profile_keeps_default_exit_spec(tmp_path: Path) -> None:
    """Back-compat: a row WITHOUT a profile sets NO v2 fields (all defaults).

    The compiled ExitSpec carries the exact pre-bridge v2 defaults, so existing
    active_plan compilation is unchanged when no profile is present.
    """
    from bhiksha.config.models import ExitSpec

    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    sheet_path = tmp_path / "sheet.csv"
    _write_csv(
        sheet_path,
        [
            {
                "row_id": "spy_manual_no_profile",
                "row_type": "manual",
                "manual_setup_type": "manual_trigger",
                "symbol": "SPY",
                "authorization_mode": "shadow",
                "direction": "long",
                "trigger_price": "602.10",
                "trigger_direction": "ABOVE",
                "profit_target_multiple": "2.0",
                "stop_loss_pct": "0.35",
            }
        ],
    )

    compiled = compile_active_plan_from_sheet(
        sheet_path=sheet_path,
        strategy_catalog_path=catalog_root,
        trading_date="2026-06-14",
    )

    assert compiled.plan.suppressed == []
    exit_spec = compiled.plan.deployments[0].exit

    # Every v2 profile dial is at its ExitSpec default (i.e. unset by the bridge).
    defaults = ExitSpec()
    for field_name in (
        "profile_exit_id",
        "profile_exit_drives_live",
        "profile_exit_shadow_only",
        "target_1_r",
        "target_2_r",
        "target_1_quantity",
        "initial_stop_pct",
        "premium_disaster_stop_pct",
        "no_progress_seconds",
        "max_hold_seconds",
        "high_water_giveback_policy",
        "breakeven_after_t1",
        "eod_flat",
        "no_progress_favorable_floor_r",
    ):
        assert getattr(exit_spec, field_name) == getattr(defaults, field_name), field_name


def test_compile_active_plan_from_google_sheets_uses_catalog_active_and_manual_tabs(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_v1",
                "playbook_id": "pb_spy_01",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "strategy_family": "impulse",
                "bhiksha_ready": "TRUE",
                "expectancy": "1.42",
                "confidence": "0.67",
                "thesis_exit_policy": "market_impulse_reclaim",
                "steward_recommendation": "shadow",
                "steward_notes": '{"rank":2,"reason":"advisory only"}',
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategies",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "live",
                "strategy": "market_impulse_spy_short_v1",
                "max_premium": "180",
                "start": "09:40",
            }
        ],
    )
    manual_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="manual_entry",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "shadow",
                "strategy": "breakout",
                "symbol": "SPY",
                "direction": "long",
                "trigger": "603.25",
                "trigger_when": "ABOVE",
                "after": "09:35",
                "end_in_days": "1",
                "notes": "opening breakout",
                "id": "spy_breakout_lane",
            }
        ],
    )

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategies",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_2026-04-11",
        trading_date="2026-04-11",
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.active_plan_id == "active_plan_2026-04-11"
    assert compiled.plan.source["spreadsheet_id"] == "spreadsheet123"
    assert compiled.plan.source["catalog_sheet_name"] == "strategy catalog"
    assert compiled.plan.source["strategy_sheet_name"] == "active_strategies"
    assert compiled.plan.source["manual_sheet_name"] == "manual_entry"
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "strategy_market_impulse_spy_short_v1_live_row_2",
        "spy_breakout_lane",
    ]
    strategy = compiled.plan.deployments[0]
    manual = compiled.plan.deployments[1]
    assert strategy.source.metadata["row_index"] == 2
    assert strategy.source.metadata["catalog_key"] == "market_impulse_spy_short_v1"
    assert strategy.source.metadata["playbook_id"] == "pb_spy_01"
    assert strategy.source.metadata["expectancy"] == 1.42
    assert "steward_recommendation" not in strategy.source.metadata
    assert "steward_notes" not in strategy.source.metadata
    assert manual.source.metadata["row_index"] == 2
    assert manual.strategy.key == "manual_breakout"
    assert manual.execution.dte_max == 1
    assert manual.exit.stop_loss_pct == 0.35
    assert manual.exit.profit_target_multiple == 1.25
    assert manual.exit.hard_flat_time_et == "15:53"
    assert manual.source.metadata["manual_setup_type"] == "breakout"
    assert manual.source.metadata["notes"] == "opening breakout"


def test_compile_active_plan_from_google_sheets_suppresses_non_ready_catalog_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_impulse.yaml", strategy_id="market_impulse_spy_short_v1", symbol="SPY")

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_v1",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "bhiksha_ready": "FALSE",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategies",
        rows=[{"strategy": "market_impulse_spy_short_v1"}],
    )
    manual_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="manual_entry",
        rows=[],
    )

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategies",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not Bhiksha runtime-ready" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_from_google_sheets_promotes_google_catalog_entries(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_19383a3c9faf",
                "playbook_id": "market_impulse_spy_short_17d4462c5932",
                "symbol": "SPY",
                "bias_template": "bearish_trend_intraday",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "active",
                "bhiksha_ready": "TRUE",
                "last_validated_date": "2026-04-01",
                "thesis_exit_policy": "fixed_rr_underlying",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {
                            "direction": "short",
                            "entry_buffer_minutes": 3,
                            "entry_window_minutes": 45,
                            "regime_timeframe": "1h",
                        },
                        "vehicle_mapping": {
                            "profile": "single_leg_long_premium_v1",
                            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                            "dte_min": 0,
                            "dte_max": 7,
                            "target_abs_delta_min": 0.2,
                            "target_abs_delta_max": 0.4,
                        },
                        "catastrophe_exit_params": {
                            "hard_flat_time_et": "15:55",
                            "stop_loss_pct": 0.45,
                            "use_profit_target": False,
                        },
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0035,
                            "take_profit_underlying_r_multiple": 1.5,
                        },
                    }
                ),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[{"enabled": "TRUE", "mode": "live", "strategy": "market_impulse_spy_short_19383a3c9faf"}],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    generated_path = catalog_root / "google_promoted" / "market_impulse_spy_short_19383a3c9faf.yaml"
    assert generated_path.exists()
    generated_payload = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    assert generated_payload["strategy_id"] == "market_impulse_spy_short_19383a3c9faf"
    assert generated_payload["strategy"]["key"] == "market_impulse"
    assert generated_payload["exit"]["thesis_exit_policy"] == "fixed_rr_underlying"
    assert generated_payload["exit"]["use_algorithmic_exit"] is False
    assert compiled.plan.deployments[0].deployment_id == "strategy_market_impulse_spy_short_19383a3c9faf_live_row_2"
    assert compiled.plan.deployments[0].strategy.key == "market_impulse"
    assert compiled.plan.deployments[0].exit.use_algorithmic_exit is False


def test_compile_active_plan_maps_mala_v2_compact_playbook_summary(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "playbook_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "candidate",
                "bhiksha_ready": "TRUE",
                "operator_status_override": "shadow",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {
                            "entry_buffer_minutes": 5,
                            "entry_window_minutes": 90,
                            "regime_timeframe": "1h",
                            "vwma_periods": [5, 13, 21],
                        },
                        "bhiksha_compatibility": {
                            "supported": False,
                            "note": "mala_v2 candidate — pending bhiksha config review",
                        },
                        "vehicle_mapping": {
                            "structure": "long_put",
                            "dte": "7-21",
                            "delta_plan": "long 0.35-0.55 / short 0.10-0.25",
                            "entry_window_et": "09:45-14:30",
                            "risk_rule": "hard stop at -35% premium",
                        },
                        "catastrophe_exit_params": {
                            "hard_flat_time_et": "15:55",
                            "stop_loss_pct": 0.35,
                        },
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0075,
                            "take_profit_underlying_r_multiple": 2.0,
                        },
                    }
                ),
                "thesis_exit_policy": "fixed_rr_underlying",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "mode": "shadow",
                "strategy": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    deployment = compiled.plan.deployments[0]
    assert deployment.strategy.params["vwma_periods"] == [5, 13, 21]
    assert deployment.execution.dte_min == 7
    assert deployment.execution.dte_max == 21
    assert deployment.execution.target_abs_delta_min == 0.35
    assert deployment.execution.target_abs_delta_max == 0.55
    assert deployment.execution.entry_window_start_et == "09:45"
    assert deployment.execution.entry_window_end_et == "14:30"
    assert deployment.risk.stop_loss_pct == 0.35
    assert deployment.exit.thesis_exit_policy == "fixed_rr_underlying"
    assert deployment.exit.use_algorithmic_exit is False
    assert deployment.exit.thesis_exit_params == {
        "stop_loss_underlying_pct": 0.0075,
        "take_profit_underlying_r_multiple": 2.0,
    }
    compatibility = deployment.source.metadata["playbook_summary"]["bhiksha_compatibility"]
    assert compatibility["bhiksha_ready"] is True
    assert compatibility["supported"] is True
    assert compatibility["note"] == "bhiksha strategy and exit policy both implemented"


def test_compile_active_plan_can_use_mala_evidence_and_operator_defaults(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__iwm_long",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "IWM",
                "direction": "long",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_params_json": json.dumps(
                    {
                        "entry_buffer_minutes": 5,
                        "entry_window_minutes": 45,
                        "regime_timeframe": "15m",
                        "vwma_periods": [5, 13, 21],
                    }
                ),
                "signal_window_et": "09:35-10:15",
                "recommendation_tier": "shadow",
                "expectancy": "0.56",
                "confidence": "0.98",
                "signal_count": "49",
                "execution_robustness": "1.0",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
                "option_trade_ready": "TRUE",
                "option_adjusted_expectancy_pct": "0.18",
                "option_exit_quality": "fast_intraday",
                "recommended_dte_min": "3",
                "recommended_dte_max": "7",
                "exit_reliability": "thin",
                "warnings": "legacy_m5_execution_mapping_ignored:entry_window_et",
            }
        ],
    )
    defaults_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Operator_Defaults_v1",
        rows=[
            {"section": "default", "key": "execution_window_start_et", "value": "09:30"},
            {"section": "default", "key": "execution_window_end_et", "value": "16:00"},
            {"section": "default", "key": "max_trade_premium_usd", "value": "500"},
            {"section": "default", "key": "option_stop_pct", "value": "0.35"},
            {"section": "default", "key": "option_profit_target_enabled", "value": "TRUE"},
            {"section": "default", "key": "option_profit_target_pct", "value": "0.35"},
            {"section": "default", "key": "target_approach_offset_pct", "value": "0.10"},
            {"section": "default", "key": "target_pullback_restore_progress_pct", "value": "0.75"},
            {"section": "default", "key": "min_open_interest", "value": "25"},
            {"section": "default", "key": "max_bid_ask_spread_pct", "value": "0.10"},
            {"section": "default", "key": "entry_execution_profile", "value": "balanced"},
            {"section": "default", "key": "entry_pricing_spread_fraction", "value": "0.25"},
            {"section": "default", "key": "entry_pricing_oi_percentile_scale", "value": "TRUE"},
            {"section": "default", "key": "entry_reprice_enabled", "value": "TRUE"},
            {"section": "default", "key": "entry_reprice_checkpoints_seconds", "value": "[60, 180]"},
            {"section": "default", "key": "entry_reprice_cancel_after_seconds", "value": "300"},
            {"section": "default", "key": "entry_reprice_spread_fractions", "value": "[0.50, 0.70]"},
            {"section": "default", "key": "entry_reprice_max_chase_pct", "value": "0.12"},
            {"section": "default", "key": "dte_fallback_policy", "value": "allow_nearest_after"},
            {"section": "default", "key": "max_open_positions_total", "value": "4"},
            {"section": "default", "key": "max_open_positions_per_symbol", "value": "2"},
            {"section": "default", "key": "max_open_positions_per_deployment", "value": "1"},
            {"section": "default", "key": "dte_min", "value": "5"},
            {"section": "default", "key": "dte_max", "value": "21"},
            {"section": "default", "key": "delta_min", "value": "0.15"},
            {"section": "default", "key": "delta_max", "value": "0.40"},
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "live",
                "strategy_id": "market-impulse-all-basket-discovery__iwm_long",
                "entry_window_start_et": "09:30",
                "max_trade_premium_usd": "1000",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        defaults_sheet_name="Operator_Defaults_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        defaults_client=defaults_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    deployment = compiled.plan.deployments[0]
    assert deployment.strategy.params["entry_buffer_minutes"] == 5
    assert deployment.strategy.params["direction"] == "long"
    assert deployment.execution.dte_min == 3
    assert deployment.execution.dte_max == 7
    assert deployment.execution.target_abs_delta_min == 0.15
    assert deployment.execution.target_abs_delta_max == 0.40
    assert deployment.execution.min_open_interest == 25
    assert deployment.execution.max_bid_ask_spread_pct == 0.10
    assert deployment.execution.entry_execution_profile == "balanced"
    assert deployment.execution.entry_pricing_spread_fraction == 0.25
    assert deployment.execution.entry_pricing_oi_percentile_scale is True
    assert deployment.execution.entry_reprice_enabled is True
    assert deployment.execution.entry_reprice_checkpoints_seconds == [60, 180]
    assert deployment.execution.entry_reprice_cancel_after_seconds == 300
    assert deployment.execution.entry_reprice_spread_fractions == [0.50, 0.70]
    assert deployment.execution.entry_reprice_max_chase_pct == 0.12
    assert deployment.execution.dte_fallback_policy == "allow_nearest_after"
    assert deployment.execution.entry_window_start_et == "09:30"
    assert deployment.execution.entry_window_end_et == "16:00"
    assert deployment.risk.max_trade_premium_usd == 1000
    assert deployment.risk.max_open_positions_total == 4
    assert deployment.risk.max_open_positions_per_symbol == 2
    assert deployment.risk.max_open_positions_per_deployment == 1
    assert deployment.risk.stop_loss_pct == 0.35
    assert deployment.exit.use_algorithmic_exit is False
    assert deployment.exit.use_profit_target is True
    assert deployment.exit.option_profit_target_pct == 0.35
    assert deployment.exit.target_approach_offset_pct == 0.10
    assert deployment.exit.target_pullback_restore_progress_pct == 0.75
    assert deployment.exit.profit_target_multiple is None
    assert deployment.exit.thesis_exit_params == {
        "stop_loss_underlying_pct": 0.005,
        "take_profit_underlying_r_multiple": 2.0,
    }
    assert deployment.source.metadata["mala_handoff_version"] == 1
    assert deployment.source.metadata["strategy_variant"] == "cross_reclaim"
    assert deployment.source.metadata["bhiksha_capability_status"] == "supported"
    assert deployment.source.metadata["signal_window_et"] == "09:35-10:15"
    assert deployment.source.metadata["option_trade_ready"] is True
    assert deployment.source.metadata["option_adjusted_expectancy_pct"] == 0.18
    assert deployment.source.metadata["recommended_dte_min"] == 3
    assert deployment.source.metadata["recommended_dte_max"] == 7
    # operator-audit P3: the compiled plan carries the flat operator_defaults
    # dict through so the live runtime can build a PlanOperatorDefaultsSource
    # without a second Sheet read at session start.
    assert compiled.plan.operator_defaults["option_stop_pct"] == "0.35"
    assert compiled.plan.operator_defaults["dte_min"] == "5"
    plan_payload = compiled.plan.model_dump(mode="json")
    assert plan_payload["operator_defaults"]["option_stop_pct"] == "0.35"


def test_active_plan_operator_defaults_round_trips_through_load_active_plan(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "spy_jerk.yaml", strategy_id="spy_jerk_pivot_short_v1", symbol="SPY")

    compiled = compile_active_plan_from_rows(
        rows=[],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_test",
        trading_date="2026-07-02",
        operator_defaults={"risk_max_daily_drawdown_pct": "1.5", "max_daily_drawdown_pct": "1.75"},
    )
    assert compiled.plan.operator_defaults == {
        "risk_max_daily_drawdown_pct": "1.5",
        "max_daily_drawdown_pct": "1.75",
    }

    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    output_path.parent.mkdir(parents=True)
    output_path.write_text(json.dumps(compiled.plan.model_dump(mode="json")), encoding="utf-8")

    reloaded = load_active_plan(output_path)
    assert reloaded.operator_defaults["max_daily_drawdown_pct"] == "1.75"


def test_compile_active_plan_from_rows_defaults_operator_defaults_to_empty_dict(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    compiled = compile_active_plan_from_rows(
        rows=[],
        strategy_catalog_path=catalog_root,
        active_plan_id="active_plan_no_defaults",
        trading_date="2026-07-02",
    )

    assert compiled.plan.operator_defaults == {}


def test_mala_evidence_preserves_explicit_bhiksha_ready_when_provider_columns_are_advisory(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "bhiksha_capability_status": "supported",
                "bhiksha_capability_reason": "runtime_verified",
                "provider_validation_status": "provider_watch",
                "provider_feature_risk": "yellow",
                "provider_signal_overlap": "0.97",
                "bhiksha_runtime_supported": "TRUE",
                "mala_evidence_ready": "TRUE",
                "activation_candidate": "TRUE",
                "m7_status": "provider_watch",
                "m7_feature_risk": "yellow",
                "m7_signal_overlap": "0.97",
                "triage_verdict": "CLEAN",
                "triage_blocking_checks": "none",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.summary["suppressed_count"] == 0
    assert [deployment.deployment_id for deployment in compiled.plan.deployments] == [
        "strategy_market_impulse_all_basket_discovery_amd_short_shadow_row_2"
    ]
    metadata = compiled.plan.deployments[0].source.metadata
    assert metadata["bhiksha_ready"] is True
    assert metadata["provider_validation_status"] == "provider_watch"
    assert metadata["provider_signal_overlap"] == 0.97
    assert metadata["activation_candidate"] is True
    assert metadata["triage_verdict"] == "CLEAN"


def test_compile_active_plan_suppresses_mala_evidence_when_activation_candidate_false(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "bhiksha_capability_status": "supported",
                "bhiksha_capability_reason": "runtime_verified",
                "activation_candidate": "FALSE",
                "activation_blocking_checks": "m7_signal_overlap=0.875; required>=0.95_for_activation",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "live",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not activation_candidate" in compiled.plan.suppressed[0]["reason"]


def test_mala_evidence_watch_only_runtime_supported_fails_on_activation_not_runtime_readiness(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_iwm.yaml",
        strategy_id="market-impulse-all-basket-discovery__iwm_long",
        symbol="IWM",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__iwm_long",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "IWM",
                "direction": "long",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "long"}),
                "recommendation_tier": "watch_only",
                "bhiksha_ready": "FALSE",
                "bhiksha_runtime_supported": "TRUE",
                "bhiksha_capability_status": "supported",
                "activation_candidate": "FALSE",
                "activation_blocking_checks": "mala_recommendation_tier=watch_only",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "live",
                "strategy_id": "market-impulse-all-basket-discovery__iwm_long",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not activation_candidate" in compiled.plan.suppressed[0]["reason"]
    assert "not Bhiksha runtime-ready" not in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_mala_evidence_when_option_trade_not_ready(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "activation_candidate": "TRUE",
                "option_trade_ready": "FALSE",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "live",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not option_trade_ready" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_mala_evidence_without_thesis_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "market-impulse-all-basket-discovery__amd_short",
                "hypothesis_id": "market-impulse-all-basket-discovery",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "Market Impulse (Cross & Reclaim)",
                "strategy_variant": "cross_reclaim",
                "strategy_params_json": json.dumps({"direction": "short"}),
                "recommendation_tier": "shadow",
                "bhiksha_ready": "TRUE",
                "bhiksha_capability_status": "supported",
                "bhiksha_capability_reason": "runtime_verified",
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "exit_contract_missing" in compiled.plan.suppressed[0]["reason"]


def test_compile_active_plan_suppresses_unsupported_mala_strategy_variant(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "mi_second_touch.yaml",
        strategy_id="mi-desc-second-touch-semiconductors-m1__amd_short",
        symbol="AMD",
    )

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[
            {
                "mala_handoff_version": "1",
                "catalog_key": "mi-desc-second-touch-semiconductors-m1__amd_short",
                "hypothesis_id": "mi-desc-second-touch-semiconductors-m1",
                "symbol": "AMD",
                "direction": "short",
                "strategy_key": "market_impulse",
                "strategy_name": "MI Second Touch",
                "strategy_params_json": json.dumps(
                    {
                        "entry_mode": "delayed_reclaim",
                        "reclaim_window_bars": 3,
                        "min_bars_after_pierce": 1,
                        "entry_buffer_minutes": 3,
                        "entry_window_minutes": 60,
                    }
                ),
                "recommendation_tier": "shadow",
                "thesis_exit_tested": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "thesis_exit_params_json": json.dumps(
                    {
                        "stop_loss_underlying_pct": 0.005,
                        "take_profit_underlying_r_multiple": 2.0,
                    }
                ),
                "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": "shadow",
                "strategy_id": "mi-desc-second-touch-semiconductors-m1__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "unsupported_strategy_variant: market_impulse.delayed_reclaim" in compiled.plan.suppressed[0]["reason"]


def test_google_catalog_exit_controls_can_explicitly_enable_native_exit(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()

    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        rows=[
            {
                "catalog_key": "market_impulse_spy_short_native_exit",
                "playbook_id": "playbook_123",
                "symbol": "SPY",
                "strategy_key": "market_impulse",
                "strategy_family": "market_impulse",
                "direction": "short",
                "lifecycle_status": "active",
                "bhiksha_ready": "TRUE",
                "thesis_exit_policy": "fixed_rr_underlying",
                "playbook_summary_json": json.dumps(
                    {
                        "entry_params": {"direction": "short"},
                        "vehicle_mapping": {"profile": "single_leg_long_premium_v1"},
                        "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
                        "thesis_exit_params": {
                            "stop_loss_underlying_pct": 0.0035,
                            "take_profit_underlying_r_multiple": 1.5,
                        },
                        "exit_controls": {"use_algorithmic_exit": True},
                    }
                ),
            }
        ],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[{"enabled": "TRUE", "mode": "live", "strategy": "market_impulse_spy_short_native_exit"}],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])

    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="strategy catalog",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )

    assert compiled.plan.deployments[0].exit.use_algorithmic_exit is True


def test_sync_google_strategy_catalog_writes_active_or_candidate_bhiksha_ready_supported_rows(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    (catalog_root / "manual.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": "manual_preserved",
                "enabled": True,
                "symbol": "SPY",
                "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
                "execution": {"profile": "single_leg_long_premium_v1"},
                "risk": {"profile": "conservative_day1"},
                "exit": {"profile": "strategy_exit_v1"},
                "source": {"origin": "manual"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    written = sync_google_strategy_catalog(
        strategy_catalog_path=catalog_root,
        google_strategy_catalog=[
            _catalog_sheet_row(
                catalog_key="eligible_market_impulse",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=True,
            ),
            _catalog_sheet_row(
                catalog_key="candidate_shadow_market_impulse",
                symbol="AMD",
                strategy_key="market_impulse",
                lifecycle_status="candidate",
                bhiksha_ready=True,
                operator_status_override="shadow",
            ),
            _catalog_sheet_row(
                catalog_key="not_ready",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=False,
            ),
            _catalog_sheet_row(
                catalog_key="retired",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="retired",
                bhiksha_ready=True,
            ),
            _catalog_sheet_row(
                catalog_key="unsupported",
                symbol="SPY",
                strategy_key="not_in_registry",
                lifecycle_status="active",
                bhiksha_ready=True,
            ),
        ],
    )

    assert [path.name for path in written] == [
        "eligible_market_impulse.yaml",
        "candidate_shadow_market_impulse.yaml",
    ]
    assert (catalog_root / "google_promoted" / "eligible_market_impulse.yaml").exists()
    assert (catalog_root / "google_promoted" / "candidate_shadow_market_impulse.yaml").exists()
    assert not (catalog_root / "google_promoted" / "not_ready.yaml").exists()
    assert not (catalog_root / "google_promoted" / "retired.yaml").exists()
    assert not (catalog_root / "google_promoted" / "unsupported.yaml").exists()


def test_sync_google_strategy_catalog_preserves_existing_file_when_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    generated_root = catalog_root / "google_promoted"
    generated_root.mkdir(parents=True)
    existing_path = generated_root / "eligible_market_impulse.yaml"
    existing_path.write_text("strategy_id: previous\n", encoding="utf-8")

    def _fail_write(path: Path, payload: str) -> None:
        del path, payload
        raise OSError("disk full")

    monkeypatch.setattr("bhiksha.active_plan.compiler._atomic_yaml_write", _fail_write)

    with pytest.raises(OSError, match="disk full"):
        sync_google_strategy_catalog(
            strategy_catalog_path=catalog_root,
            google_strategy_catalog=[
                _catalog_sheet_row(
                    catalog_key="eligible_market_impulse",
                    symbol="SPY",
                    strategy_key="market_impulse",
                    lifecycle_status="active",
                    bhiksha_ready=True,
                )
            ],
        )

    assert existing_path.read_text(encoding="utf-8") == "strategy_id: previous\n"


def test_google_catalog_payload_preserves_explicit_zero_execution_limits(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"

    sync_google_strategy_catalog(
        strategy_catalog_path=catalog_root,
        google_strategy_catalog=[
            _catalog_sheet_row(
                catalog_key="zero_dte_market_impulse",
                symbol="SPY",
                strategy_key="market_impulse",
                lifecycle_status="active",
                bhiksha_ready=True,
                playbook_summary_json={
                    "entry_params": {"direction": "short"},
                    "vehicle_mapping": {
                        "profile": "single_leg_long_premium_v1",
                        "dte_min": 0,
                        "dte_max": 0,
                        "min_open_interest": 0,
                        "target_abs_delta_min": 0,
                        "target_abs_delta_max": 0,
                    },
                    "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
                },
            )
        ],
        operator_defaults={"dte_max": 7, "min_open_interest": 100},
    )

    payload = yaml.safe_load((catalog_root / "google_promoted" / "zero_dte_market_impulse.yaml").read_text(encoding="utf-8"))
    assert payload["execution"]["dte_min"] == 0
    assert payload["execution"]["dte_max"] == 0
    assert payload["execution"]["min_open_interest"] == 0
    assert payload["execution"]["target_abs_delta_min"] == 0
    assert payload["execution"]["target_abs_delta_max"] == 0


def test_compile_active_plan_cli_supports_google_sheets_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(catalog_root / "qqq_impulse.yaml", strategy_id="qqq_market_impulse_short_v1", symbol="QQQ")
    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"

    def _fake_compile(**kwargs):
        assert kwargs["defaults_sheet_name"] == "Operator_Defaults_v1"
        return compile_active_plan_from_google_sheets(
            spreadsheet_id="spreadsheet123",
            credentials_path=tmp_path / "credentials.json",
            catalog_sheet_name="strategy catalog",
            strategy_sheet_name="active_strategies",
            manual_sheet_name="manual_entry",
            strategy_catalog_path=kwargs["strategy_catalog_path"],
            active_plan_id=kwargs["active_plan_id"],
            trading_date=kwargs["trading_date"],
            catalog_client=_FakeSheetClient(
                spreadsheet_id="spreadsheet123",
                sheet_name="strategy catalog",
                rows=[
                    {
                        "catalog_key": "qqq_market_impulse_short_v1",
                        "symbol": "QQQ",
                        "strategy_key": "market_impulse",
                        "bhiksha_ready": "TRUE",
                    }
                ],
            ),
            strategy_client=_FakeSheetClient(
                spreadsheet_id="spreadsheet123",
                sheet_name="active_strategies",
                rows=[{"strategy": "qqq_market_impulse_short_v1"}],
            ),
            manual_client=_FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[]),
        )

    monkeypatch.setattr("bhiksha.tools.compile_active_plan.compile_active_plan_from_google_sheets", _fake_compile)

    exit_code = compile_active_plan_main(
        [
            "--google-sheet-id",
            "spreadsheet123",
            "--credentials-path",
            str(tmp_path / "credentials.json"),
            "--catalog-sheet-name",
            "strategy catalog",
            "--strategy-catalog",
            str(catalog_root),
            "--out",
            str(output_path),
            "--active-plan-id",
            "active_plan_2026-04-11",
            "--trading-date",
            "2026-04-11",
        ]
    )

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-11"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["strategy_qqq_market_impulse_short_v1_shadow_row_2"]


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_catalog_entry(path: Path, *, strategy_id: str, symbol: str) -> None:
    payload = {
        "strategy_id": strategy_id,
        "enabled": True,
        "symbol": symbol,
        "strategy": {
            "key": "jerk_pivot_momentum" if "jerk" in strategy_id else "market_impulse",
            "version": 1,
            "params": {"direction": "short"},
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": True,
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": 7,
            "target_abs_delta_min": 0.2,
            "target_abs_delta_max": 0.4,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.2,
        },
        "risk": {
            "profile": "conservative_day1",
            "max_trade_premium_usd": 300,
            "hard_flat_time_et": "15:55",
            "stop_loss_pct": 0.45,
        },
        "exit": {
            "profile": "strategy_exit_v1",
            "use_algorithmic_exit": "jerk" not in strategy_id,
            "use_profit_target": False,
            "profit_target_multiple": None,
            "stop_loss_pct": 0.45,
            "stop_to_breakeven_after_r_multiple": None,
            "hard_flat_time_et": "15:55",
        },
        "source": {"origin": "test_catalog", "run_date": "2026-04-08", "artifact": "research.md"},
        "approval_status": "approved",
        "tags": ["test"],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class _FakeSheetClient:
    def __init__(self, *, spreadsheet_id: str, sheet_name: str, rows: list[dict[str, str]]) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._rows = rows

    def read_rows(self, *, range_suffix: str = "A1:ZZ2000") -> list[dict[str, str]]:
        del range_suffix
        return [
            {
                **row,
                "row_index": index,
            }
            for index, row in enumerate(self._rows, start=2)
        ]


def _catalog_sheet_row(**overrides):
    payload = {
        "catalog_key": "market_impulse_spy_short_19383a3c9faf",
        "playbook_id": "playbook_123",
        "symbol": "SPY",
        "bias_template": "bearish_trend_intraday",
        "strategy_key": "market_impulse",
        "strategy_family": "market_impulse",
        "direction": "short",
        "lifecycle_status": "active",
        "bhiksha_ready": True,
        "playbook_summary_json": {
            "entry_params": {"direction": "short"},
            "vehicle_mapping": {"profile": "single_leg_long_premium_v1"},
            "catastrophe_exit_params": {"hard_flat_time_et": "15:55", "stop_loss_pct": 0.45},
        },
    }
    payload.update(overrides)
    from bhiksha.active_plan.compiler import StrategyCatalogSheetRow

    return StrategyCatalogSheetRow.model_validate(payload)


def _evidence_gate_catalog_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "mala_handoff_version": "1",
        "catalog_key": "market-impulse-all-basket-discovery__amd_short",
        "hypothesis_id": "market-impulse-all-basket-discovery",
        "symbol": "AMD",
        "direction": "short",
        "strategy_key": "market_impulse",
        "strategy_name": "Market Impulse (Cross & Reclaim)",
        "strategy_variant": "cross_reclaim",
        "strategy_params_json": json.dumps({"direction": "short"}),
        "recommendation_tier": "watch_only",
        "lifecycle_status": "candidate",
        "bhiksha_ready": "TRUE",
        "bhiksha_capability_status": "supported",
        "bhiksha_capability_reason": "runtime_verified",
        "mala_evidence_ready": "FALSE",
        "mala_evidence_blocking_checks": "recommendation_tier=watch_only",
        "activation_candidate": "FALSE",
        "activation_blocking_checks": "m7_signal_overlap=0.875; required>=0.95_for_activation",
        "option_trade_ready": "FALSE",
        "thesis_exit_tested": "TRUE",
        "thesis_exit_policy": "fixed_rr_underlying",
        "thesis_exit_params_json": json.dumps(
            {
                "stop_loss_underlying_pct": 0.005,
                "take_profit_underlying_r_multiple": 2.0,
            }
        ),
        "thesis_exit_metrics_json": json.dumps({"expectancy": 0.56, "profit_factor": 2.0}),
    }
    row.update(overrides)
    return row


def _compile_single_evidence_gate_row(
    tmp_path: Path, *, catalog_overrides: dict[str, object] | None = None, authorization_mode: str = "shadow"
):
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir(exist_ok=True)
    _write_catalog_entry(
        catalog_root / "mi_amd.yaml",
        strategy_id="market-impulse-all-basket-discovery__amd_short",
        symbol="AMD",
    )
    catalog_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="Mala_Evidence_v1",
        rows=[_evidence_gate_catalog_row(**(catalog_overrides or {}))],
    )
    strategy_client = _FakeSheetClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategy",
        rows=[
            {
                "enabled": "TRUE",
                "authorization_mode": authorization_mode,
                "strategy_id": "market-impulse-all-basket-discovery__amd_short",
            }
        ],
    )
    manual_client = _FakeSheetClient(spreadsheet_id="spreadsheet123", sheet_name="manual_entry", rows=[])
    return compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=catalog_client,
        strategy_client=strategy_client,
        manual_client=manual_client,
    )


def test_shadow_row_compiles_with_relaxed_evidence_gates(tmp_path: Path) -> None:
    """Shadow rows accept candidate-grade evidence; the relaxation is stamped into metadata."""
    compiled = _compile_single_evidence_gate_row(tmp_path)

    assert compiled.plan.summary["suppressed_count"] == 0
    assert len(compiled.plan.deployments) == 1
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.shadow_only is True
    relaxed = deployment.source.metadata["evidence_gates_relaxed"]
    assert [gate.split(":")[0] for gate in relaxed] == [
        "mala_evidence_ready",
        "activation_candidate",
        "option_trade_ready",
    ]
    assert "m7_signal_overlap=0.875" in relaxed[1]


def test_live_row_still_suppressed_on_relaxed_evidence_gates(tmp_path: Path) -> None:
    """The same sub-activation row in live mode keeps the full evidence bar."""
    compiled = _compile_single_evidence_gate_row(tmp_path, authorization_mode="live")

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "not mala_evidence_ready" in compiled.plan.suppressed[0]["reason"]


def test_shadow_row_still_suppressed_on_safety_gates(tmp_path: Path) -> None:
    """Evidence gates relax for shadow; safety/integrity gates never do."""
    kill = _compile_single_evidence_gate_row(
        tmp_path, catalog_overrides={"triage_verdict": "KILL", "triage_verdict_reason": "regime artifact"}
    )
    assert kill.plan.deployments == []
    assert "triage_verdict=KILL" in kill.plan.suppressed[0]["reason"]
    assert kill.plan.summary["coverage"][
        "intentional_pre_observation_suppression_count"
    ] == 1
    assert kill.plan.summary["coverage"]["unexpected_coverage_loss_count"] == 0
    assert kill.plan.summary["coverage"]["release_safe"] is True

    blocked = _compile_single_evidence_gate_row(tmp_path, catalog_overrides={"m7_status": "block"})
    assert blocked.plan.deployments == []
    assert "m7_status=block" in blocked.plan.suppressed[0]["reason"]
    assert blocked.plan.suppressed[0]["suppression_class"] == "policy_gate"
    assert blocked.plan.summary["coverage"]["policy_gate_suppression_count"] == 1
    assert blocked.plan.summary["coverage"]["unexpected_coverage_loss_count"] == 0
    assert blocked.plan.summary["coverage"]["release_safe"] is True

    not_ready = _compile_single_evidence_gate_row(tmp_path, catalog_overrides={"bhiksha_ready": "FALSE"})
    assert not_ready.plan.deployments == []
    assert "not Bhiksha runtime-ready" in not_ready.plan.suppressed[0]["reason"]
    assert not_ready.plan.suppressed[0]["suppression_class"] == "policy_gate"
    assert not_ready.plan.summary["coverage"]["policy_gate_suppression_count"] == 1
    assert not_ready.plan.summary["coverage"]["unexpected_coverage_loss_count"] == 0
    assert not_ready.plan.summary["coverage"]["release_safe"] is True


def test_enabled_google_strategy_row_missing_strategy_id_is_unsafe_coverage_loss(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    compiled = compile_active_plan_from_google_sheets(
        spreadsheet_id="spreadsheet123",
        credentials_path=tmp_path / "credentials.json",
        catalog_sheet_name="Mala_Evidence_v1",
        strategy_sheet_name="active_strategy",
        manual_sheet_name="manual_entry",
        strategy_catalog_path=catalog_root,
        catalog_client=_FakeSheetClient(
            spreadsheet_id="spreadsheet123",
            sheet_name="Mala_Evidence_v1",
            rows=[],
        ),
        strategy_client=_FakeSheetClient(
            spreadsheet_id="spreadsheet123",
            sheet_name="active_strategy",
            rows=[{"enabled": True, "mode": "shadow", "symbol": "SMH"}],
        ),
        manual_client=_FakeSheetClient(
            spreadsheet_id="spreadsheet123",
            sheet_name="manual_entry",
            rows=[],
        ),
    )

    assert compiled.plan.deployments == []
    assert "missing strategy_id" in compiled.plan.suppressed[0]["reason"]
    coverage = compiled.plan.summary["coverage"]
    assert coverage["expected_enabled_row_count"] == 1
    assert coverage["unexpected_coverage_loss_count"] == 1
    assert coverage["release_safe"] is False


def test_auto_reconcile_uses_exact_row_ids_not_suffix_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bhiksha.evidence.bindings import build_registry_payload
    import bhiksha.active_plan.compiler as compiler_module

    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    _write_catalog_entry(
        catalog_root / "short.yaml", strategy_id="strategy-short", symbol="SPY"
    )
    _write_catalog_entry(
        catalog_root / "long.yaml", strategy_id="strategy-long", symbol="QQQ"
    )
    rows = [
        ActivePlanSheetRow.model_validate(
            {
                "row_id": "row",
                "row_type": "strategy",
                "strategy_id": "strategy-short",
                "symbol": "SPY",
            }
        ),
        ActivePlanSheetRow.model_validate(
            {
                "row_id": "prefix-row",
                "row_type": "strategy",
                "strategy_id": "strategy-long",
                "symbol": "QQQ",
            }
        ),
    ]
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(json.dumps(build_registry_payload([])))
    packet_root = tmp_path / "packets"
    packet_root.mkdir()

    def fake_reconcile(**kwargs):
        mapped = kwargs["rows_by_id"]
        assert mapped["row"].strategy_id == "strategy-short"
        assert mapped["prefix-row"].strategy_id == "strategy-long"
        return {"created": [], "reused": [], "blocked": [], "bindings": {}}

    monkeypatch.setattr(compiler_module, "reconcile_shadow_experiments", fake_reconcile)

    compiled = compile_active_plan_from_rows(
        rows=rows,
        strategy_catalog_path=catalog_root,
        evidence_bindings={},
        auto_reconcile_shadow_experiments=True,
        auto_experiment_packet_root=packet_root,
        auto_experiment_bindings_path=bindings_path,
    )

    assert {item.deployment_id for item in compiled.plan.deployments} == {
        "row",
        "prefix-row",
    }
    assert compiled.plan.summary["coverage"]["release_safe"] is True
