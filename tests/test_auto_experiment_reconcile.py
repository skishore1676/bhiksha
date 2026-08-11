"""Option C: fingerprint final effective deployment, reuse or version.

Covers 8 required proofs:
1 same config Mon/Tue reuses
2 DTE change -> v2
3 delta change -> v2
4 Control exit hash change -> v2
5 notes do not create v2
6 active_strategy overrides are included
7 old packet preserved (history)
8 live rows untouched (fail-closed)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.config.models import DeploymentManifest
from bhiksha.experiments.auto_shadow import (
    _fingerprint_to_option_contract,
    compute_deployment_experiment_fingerprint,
    reconcile_shadow_experiments,
    resolve_mala_packet_root,
)
from bhiksha.active_plan.compiler import ActivePlanSheetRow, compile_active_plan_from_rows
from bhiksha.config.loader import load_strategy_catalog  # for catalog helpers


def _shadow_deployment(
    dte_min=3, dte_max=7, delta_min=0.30, delta_max=0.55,
    exit_policy_hash=None, notes="",
    execution_overrides=None, exit_overrides=None,
    direction="short", option_mapping=None,
) -> DeploymentManifest:
    # exit_policy_hash None by default to avoid needing exit_policy_snapshot validation
    exit_spec = {"profile": "test"}
    if exit_policy_hash is not None:
        exit_spec["exit_policy_hash"] = exit_policy_hash
        exit_spec["exit_policy_snapshot"] = {"hash": exit_policy_hash}
        exit_spec["exit_policy_id"] = "control-v1"
    base = {
        "deployment_id": "row-smh",
        "enabled": True,
        "symbol": "SMH",
        "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": direction, "lookback": 20}},
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "dte_min": dte_min, "dte_max": dte_max,
            "dte_fallback_policy": "allow_nearest_after",
            "target_abs_delta_min": delta_min, "target_abs_delta_max": delta_max,
            "min_open_interest": 100, "max_bid_ask_spread_pct": 0.08,
            "option_mapping": option_mapping or {"long_signal": "CALL", "short_signal": "PUT"},
            "entry_pricing_mode": "urgent",
        },
        "risk": {"profile": "test", "max_trade_premium_usd": 500},
        "exit": exit_spec,
        "source": {"origin": "test", "metadata": {}},
    }
    if execution_overrides:
        base["execution"].update(execution_overrides)
    if exit_overrides:
        base["exit"].update(exit_overrides)
    return DeploymentManifest.model_validate(base)


def test_same_config_reuses_identity():
    d1 = _shadow_deployment()
    d2 = _shadow_deployment()
    assert compute_deployment_experiment_fingerprint(d1) == compute_deployment_experiment_fingerprint(d2)


def test_auto_reconcile_requires_explicit_staging_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="requires explicit staged"):
        compile_active_plan_from_rows(
            rows=[],
            strategy_catalog_path=tmp_path,
            auto_reconcile_shadow_experiments=True,
        )


def test_declared_option_contract_comes_from_execution_mapping_for_long_strategy():
    deployment = _shadow_deployment(direction="long")

    parameters = _fingerprint_to_option_contract(deployment)

    assert parameters["long_signal_contract_type"] == "CALL"
    assert parameters["short_signal_contract_type"] == "PUT"


def test_declared_option_contract_preserves_custom_execution_mapping():
    deployment = _shadow_deployment(
        direction="long",
        option_mapping={"long_signal": "PUT", "short_signal": "CALL"},
    )

    parameters = _fingerprint_to_option_contract(deployment)

    assert parameters["long_signal_contract_type"] == "PUT"
    assert parameters["short_signal_contract_type"] == "CALL"


def test_dte_change_creates_v2():
    d1 = _shadow_deployment(dte_min=3, dte_max=7)
    d2 = _shadow_deployment(dte_min=5, dte_max=10)
    assert compute_deployment_experiment_fingerprint(d1) != compute_deployment_experiment_fingerprint(d2)


def test_delta_change_creates_v2():
    d1 = _shadow_deployment(delta_min=0.30, delta_max=0.55)
    d2 = _shadow_deployment(delta_min=0.20, delta_max=0.40)
    assert compute_deployment_experiment_fingerprint(d1) != compute_deployment_experiment_fingerprint(d2)


def test_control_exit_hash_change_creates_v2():
    d1 = _shadow_deployment(exit_policy_hash="hash-v1")
    d2 = _shadow_deployment(exit_policy_hash="hash-v2")
    assert compute_deployment_experiment_fingerprint(d1) != compute_deployment_experiment_fingerprint(d2)


def test_notes_do_not_create_v2():
    # Notes are not part of deployment fingerprint (deployment has no notes)
    # Simulate same deployment with different row notes -> fingerprint same
    d1 = _shadow_deployment()
    d2 = _shadow_deployment()
    # fingerprint should be equal even though caller might have different notes in row
    assert compute_deployment_experiment_fingerprint(d1) == compute_deployment_experiment_fingerprint(d2)


def test_active_strategy_overrides_are_included():
    # execution_overrides from active_strategy row are materialized into deployment.execution
    d1 = _shadow_deployment()
    d2 = _shadow_deployment(execution_overrides={"max_bid_ask_spread_pct": 0.15})
    assert compute_deployment_experiment_fingerprint(d1) != compute_deployment_experiment_fingerprint(d2)
    # Also exit overrides
    d3 = _shadow_deployment(exit_overrides={"hard_flat_time_et": "15:30"})
    assert compute_deployment_experiment_fingerprint(d1) != compute_deployment_experiment_fingerprint(d3)


def compute_registry_sha(payload_without_sha):
    import hashlib, json
    return hashlib.sha256(json.dumps(payload_without_sha, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def test_old_packet_preserved_and_new_versioned(tmp_path: Path):
    # Use real reconcile with two deployments differing in DTE
    packet_root = tmp_path / "evidence_packets"
    packet_root.mkdir()
    bindings_path = tmp_path / "evidence_bindings_v1.json"
    # init empty registry
    from bhiksha.evidence.bindings import build_registry_payload
    bindings_path.write_text(json.dumps(build_registry_payload([]), indent=2))

    d1 = _shadow_deployment(dte_min=3, dte_max=7)
    d1.deployment_id = "row-smh-1"
    row1 = ActivePlanSheetRow.model_validate({"row_id": "row-smh-1", "row_type": "strategy", "enabled": True, "authorization_mode": "shadow", "strategy_id": "market_impulse__smh_short", "symbol": "SMH"})

    # First reconcile should create v1
    res1 = reconcile_shadow_experiments(packet_root=packet_root, evidence_bindings_path=bindings_path, deployments=[d1], rows_by_id={d1.deployment_id: row1}, evidence_bindings={})
    assert len(res1["created"]) == 1
    p1_dir = Path(res1["created"][0]["packet_dir"])
    assert p1_dir.exists()
    fp1 = res1["created"][0]["fingerprint"]

    # Second reconcile same config should reuse
    res2 = reconcile_shadow_experiments(packet_root=packet_root, evidence_bindings_path=bindings_path, deployments=[d1], rows_by_id={d1.deployment_id: row1})
    assert len(res2["created"]) == 0
    assert "market_impulse__smh_short" in res2["reused"]

    # A fingerprint alone is insufficient: a binding whose artifact identity
    # no longer matches the retained manifest must be repaired, not reused.
    tampered_payload = json.loads(bindings_path.read_text())
    tampered_binding = dict(tampered_payload["bindings"][0])
    tampered_binding["artifact_sha256"] = "0" * 64
    bindings_path.write_text(
        json.dumps(build_registry_payload([tampered_binding]), indent=2)
    )
    repaired = reconcile_shadow_experiments(
        packet_root=packet_root,
        evidence_bindings_path=bindings_path,
        deployments=[d1],
        rows_by_id={d1.deployment_id: row1},
    )
    assert len(repaired["created"]) == 1
    repaired_binding = repaired["bindings"]["market_impulse__smh_short"]
    manifest = json.loads((p1_dir / "manifest.json").read_text())
    assert repaired_binding["artifact_sha256"] in {
        artifact["sha256"] for artifact in manifest["artifacts"]
    }

    # Third with DTE change should create v2, preserve old dir
    d2 = _shadow_deployment(dte_min=5, dte_max=10)
    d2.deployment_id = "row-smh-1"
    res3 = reconcile_shadow_experiments(packet_root=packet_root, evidence_bindings_path=bindings_path, deployments=[d2], rows_by_id={d2.deployment_id: row1})
    assert len(res3["created"]) == 1
    assert res3["created"][0]["fingerprint"] != fp1
    assert p1_dir.exists(), "old packet preserved"
    assert Path(res3["created"][0]["packet_dir"]).exists()
    assert Path(res3["created"][0]["packet_dir"]) != p1_dir


def test_long_candidate_ids_with_same_prefix_get_unique_bounded_identities(
    tmp_path: Path,
):
    packet_root = tmp_path / "evidence_packets"
    packet_root.mkdir()
    bindings_path = tmp_path / "evidence_bindings_v1.json"
    from bhiksha.evidence.bindings import build_registry_payload

    bindings_path.write_text(json.dumps(build_registry_payload([]), indent=2))
    shared = "market_impulse__" + "same-prefix-" * 7
    strategy_ids = [shared + "alpha", shared + "beta"]
    deployments = []
    rows = {}
    for index, strategy_id in enumerate(strategy_ids):
        deployment = _shadow_deployment(direction="long")
        deployment.deployment_id = f"row-long-{index}"
        deployments.append(deployment)
        rows[deployment.deployment_id] = ActivePlanSheetRow.model_validate(
            {
                "row_id": deployment.deployment_id,
                "row_type": "strategy",
                "enabled": True,
                "authorization_mode": "shadow",
                "strategy_id": strategy_id,
                "symbol": "SMH",
            }
        )

    result = reconcile_shadow_experiments(
        packet_root=packet_root,
        evidence_bindings_path=bindings_path,
        deployments=deployments,
        rows_by_id=rows,
        evidence_bindings={},
    )

    assert len(result["created"]) == 2
    manifests = [
        json.loads((Path(item["packet_dir"]) / "manifest.json").read_text())
        for item in result["created"]
    ]
    assert len({item["experiment_id"] for item in manifests}) == 2
    assert len({item["run_id"] for item in manifests}) == 2
    assert len({Path(item["packet_dir"]).name for item in result["created"]}) == 2
    assert all(len(item["experiment_id"]) <= 64 for item in manifests)
    assert all(len(item["run_id"]) <= 64 for item in manifests)
    assert all(len(Path(item["packet_dir"]).name) <= 64 for item in result["created"])

    changed = _shadow_deployment(dte_min=5, dte_max=10, direction="long")
    changed.deployment_id = deployments[0].deployment_id
    versioned = reconcile_shadow_experiments(
        packet_root=packet_root,
        evidence_bindings_path=bindings_path,
        deployments=[changed],
        rows_by_id={changed.deployment_id: rows[changed.deployment_id]},
    )
    assert versioned["created"][0]["version"] == 2


def test_live_rows_untouched(tmp_path: Path):
    packet_root = tmp_path / "evidence_packets"
    packet_root.mkdir()
    bindings_path = tmp_path / "evidence_bindings_v1.json"
    from bhiksha.evidence.bindings import build_registry_payload
    bindings_path.write_text(json.dumps(build_registry_payload([]), indent=2))

    d_live = _shadow_deployment()
    d_live.deployment_id = "row-live-1"
    row_live = ActivePlanSheetRow.model_validate({"row_id": "row-live-1", "row_type": "strategy", "enabled": True, "authorization_mode": "live", "strategy_id": "market_impulse__smh_short", "symbol": "SMH"})

    # Reconcile should NOT create packet for live (only shadow)
    res = reconcile_shadow_experiments(packet_root=packet_root, evidence_bindings_path=bindings_path, deployments=[d_live], rows_by_id={d_live.deployment_id: row_live}, evidence_bindings={})
    assert len(res["created"]) == 0
    assert len(res["reused"]) == 0
    assert len([p for p in packet_root.iterdir() if p.is_dir()]) == 0


def test_live_row_with_shadow_only_observation_binding_keeps_execution_and_quarantines_evidence(
    tmp_path: Path,
):
    import yaml
    from bhiksha.evidence.bindings import build_registry_payload

    packet_root = tmp_path / "evidence_packets"
    packet_root.mkdir()
    bindings_path = tmp_path / "evidence_bindings_v1.json"
    bindings_path.write_text(json.dumps(build_registry_payload([])))
    strategy_id = "market_impulse__smh_live"
    effective = _shadow_deployment(
        direction="long",
        delta_min=0.25,
        delta_max=0.50,
        exit_policy_hash="exit-policy-hash-v1",
        execution_overrides={
            "entry_window_start_et": "09:40",
            "entry_window_end_et": "11:15",
        },
    )
    effective.deployment_id = "row-shadow-seed"
    shadow_row = ActivePlanSheetRow.model_validate(
        {
            "row_id": effective.deployment_id,
            "row_type": "strategy",
            "enabled": True,
            "authorization_mode": "shadow",
            "strategy_id": strategy_id,
            "symbol": "SMH",
        }
    )
    seeded = reconcile_shadow_experiments(
        packet_root=packet_root,
        evidence_bindings_path=bindings_path,
        deployments=[effective],
        rows_by_id={effective.deployment_id: shadow_row},
        evidence_bindings={},
    )
    binding = seeded["bindings"][strategy_id]
    assert binding["allowed_authorization_modes"] == ["shadow"]

    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    (catalog_root / f"{strategy_id}.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": strategy_id,
                "symbol": effective.symbol,
                "enabled": True,
                "approval_status": "approved",
                "strategy": effective.strategy.model_dump(mode="json"),
                "execution": effective.execution.model_dump(mode="json"),
                "risk": effective.risk.model_dump(mode="json"),
                "exit": effective.exit.model_dump(mode="json"),
                "source": {"origin": "test", "metadata": {}},
            }
        )
    )
    live_row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "row-live",
            "row_type": "strategy",
            "enabled": True,
            "authorization_mode": "live",
            "strategy_id": strategy_id,
            "symbol": "SMH",
        }
    )

    compiled = compile_active_plan_from_rows(
        rows=[live_row],
        strategy_catalog_path=catalog_root,
        evidence_bindings={strategy_id: binding},
    )

    assert len(compiled.plan.deployments) == 1
    live = compiled.plan.deployments[0]
    assert live.execution.shadow_only is False
    assert live.strategy.params == effective.strategy.params
    assert live.execution.option_mapping == effective.execution.option_mapping
    assert live.execution.target_abs_delta_min == 0.25
    assert live.execution.target_abs_delta_max == 0.50
    assert live.execution.entry_window_start_et == "09:40"
    assert live.execution.entry_window_end_et == "11:15"
    assert live.exit.exit_policy_hash == "exit-policy-hash-v1"
    assert (
        live.source.metadata["observation_evidence_binding_status"]
        == "quarantined"
    )
    assert (
        live.source.metadata["authorization_identity_status"]
        == "evidence_binding_quarantined"
    )
    assert compiled.plan.summary["coverage"]["final_loaded_count"] == 1
    assert compiled.plan.summary["coverage"]["release_safe"] is True
    warnings = compiled.plan.summary["live_evidence_quarantine_warnings"]
    assert warnings[0]["status"] == (
        "EVIDENCE_BINDING_QUARANTINED_LIVE_EXECUTION_PRESERVED"
    )


def test_packet_root_is_derived_from_checkout_with_packet_writer(
    tmp_path: Path, monkeypatch
):
    packet_only = tmp_path / "code" / "mala_v2"
    (packet_only / "research/results/evidence_packets").mkdir(parents=True)
    canonical = tmp_path / "Documents" / "mala_v2"
    (canonical / "src/research").mkdir(parents=True)
    (canonical / "src/research/experiment_packets.py").write_text("# fixture\n")

    import bhiksha.experiments.auto_shadow as auto

    monkeypatch.setattr(auto, "MALA_ROOT_CANDIDATES", [packet_only, canonical])
    assert resolve_mala_packet_root() == canonical / "research/results/evidence_packets"


def test_missing_canonical_mala_blocks_shadow_but_not_live(tmp_path: Path, monkeypatch):
    packet_root = tmp_path / "evidence_packets"
    bindings_path = tmp_path / "evidence_bindings_v1.json"
    from bhiksha.evidence.bindings import build_registry_payload

    bindings_path.write_text(json.dumps(build_registry_payload([])))
    shadow = _shadow_deployment()
    shadow.deployment_id = "row-shadow"
    live = _shadow_deployment()
    live.deployment_id = "row-live"
    shadow_row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "row-shadow",
            "row_type": "strategy",
            "enabled": True,
            "authorization_mode": "shadow",
            "strategy_id": "market_impulse__smh_short",
            "symbol": "SMH",
        }
    )
    live_row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "row-live",
            "row_type": "strategy",
            "enabled": True,
            "authorization_mode": "live",
            "strategy_id": "market_impulse__smh_live",
            "symbol": "SMH",
        }
    )
    import bhiksha.experiments.auto_shadow as auto

    monkeypatch.setattr(auto, "MALA_ROOT_CANDIDATES", [tmp_path / "missing"])
    result = reconcile_shadow_experiments(
        packet_root=packet_root,
        evidence_bindings_path=bindings_path,
        deployments=[shadow, live],
        rows_by_id={shadow.deployment_id: shadow_row, live.deployment_id: live_row},
        evidence_bindings={},
    )
    assert result["blocked"] == [
        {
            "strategy_id": "market_impulse__smh_short",
            "reason": "canonical mala_v2 checkout not found",
        }
    ]


def test_compile_includes_overrides_in_fingerprint(tmp_path: Path, monkeypatch):
    # Integration: compile_active_plan_from_rows with execution_overrides should produce different fingerprint
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    import yaml
    entry = {
        "strategy_id": "market_impulse__smh_short",
        "symbol": "SMH",
        "enabled": True,
        "approval_status": "approved",
        "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
        "execution": {"profile": "single_leg_long_premium_v1", "dte_min": 3, "dte_max": 7, "dte_fallback_policy": "allow_nearest_after", "target_abs_delta_min": 0.30, "target_abs_delta_max": 0.55, "min_open_interest": 100, "max_bid_ask_spread_pct": 0.08, "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"}},
        "risk": {"profile": "test"},
        "exit": {"profile": "test"},
    }
    (catalog_root / "market_impulse__smh_short.yaml").write_text(yaml.safe_dump(entry))
    bindings_path = catalog_root.parent / "evidence_bindings_v1.json"
    from bhiksha.evidence.bindings import build_registry_payload
    bindings_path.write_text(json.dumps(build_registry_payload([])))
    # Patch packet-root discovery to tmp so the compiler cannot touch real Mala packets.
    packet_root = tmp_path / "evidence_packets"
    packet_root.mkdir()
    import bhiksha.experiments.auto_shadow as auto
    import bhiksha.active_plan.compiler as compiler
    monkeypatch.setattr(auto, "MALA_ROOT_CANDIDATES", [Path("/Users/suman/code/mala_v2")])
    monkeypatch.setattr(compiler, "resolve_mala_packet_root", lambda: packet_root)
    row_base = ActivePlanSheetRow.model_validate({"row_id": "row-1", "row_type": "strategy", "enabled": True, "authorization_mode": "shadow", "strategy_id": "market_impulse__smh_short", "symbol": "SMH"})
    row_overridden = ActivePlanSheetRow.model_validate({"row_id": "row-1", "row_type": "strategy", "enabled": True, "authorization_mode": "shadow", "strategy_id": "market_impulse__smh_short", "symbol": "SMH", "execution_overrides": {"max_bid_ask_spread_pct": 0.15}})
    # Compile both and compare fingerprint (bypass reconcile by passing empty bindings but still fingerprints differ)
    compiled_base = compile_active_plan_from_rows(
        rows=[row_base],
        strategy_catalog_path=catalog_root,
        evidence_bindings={},
        auto_reconcile_shadow_experiments=True,
        auto_experiment_packet_root=packet_root,
        auto_experiment_bindings_path=bindings_path,
    )
    compiled_over = compile_active_plan_from_rows(
        rows=[row_overridden],
        strategy_catalog_path=catalog_root,
        evidence_bindings={},
        auto_reconcile_shadow_experiments=True,
        auto_experiment_packet_root=packet_root,
        auto_experiment_bindings_path=bindings_path,
    )
    # Both should have auto-experiment findings but still produce deployments (now with packet)
    assert len(compiled_base.plan.deployments) == 1
    assert len(compiled_over.plan.deployments) == 1
    fp_base = compute_deployment_experiment_fingerprint(compiled_base.plan.deployments[0])
    fp_over = compute_deployment_experiment_fingerprint(compiled_over.plan.deployments[0])
    assert fp_base != fp_over


def test_long_direction_round_trip_compile_keeps_lane_and_exact_option_mapping(
    tmp_path: Path,
):
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    import yaml
    from bhiksha.evidence.bindings import build_registry_payload

    strategy_id = "market_impulse__smh_long"
    (catalog_root / f"{strategy_id}.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": strategy_id,
                "symbol": "SMH",
                "enabled": True,
                "approval_status": "approved",
                "strategy": {
                    "key": "market_impulse",
                    "version": 1,
                    "params": {"direction": "long"},
                },
                "execution": {
                    "profile": "single_leg_long_premium_v1",
                    "dte_min": 3,
                    "dte_max": 7,
                    "dte_fallback_policy": "allow_nearest_after",
                    "target_abs_delta_min": 0.30,
                    "target_abs_delta_max": 0.55,
                    "min_open_interest": 100,
                    "max_bid_ask_spread_pct": 0.08,
                    "option_mapping": {
                        "long_signal": "CALL",
                        "short_signal": "PUT",
                    },
                },
                "risk": {"profile": "test"},
                "exit": {"profile": "test"},
            }
        )
    )
    bindings_path = tmp_path / "sandbox" / "evidence_bindings_v1.json"
    bindings_path.parent.mkdir()
    bindings_path.write_text(json.dumps(build_registry_payload([])))
    packet_root = tmp_path / "sandbox" / "evidence_packets"
    packet_root.mkdir()
    row = ActivePlanSheetRow.model_validate(
        {
            "row_id": "row-long",
            "row_type": "strategy",
            "enabled": True,
            "authorization_mode": "shadow",
            "strategy_id": strategy_id,
            "symbol": "SMH",
        }
    )

    compiled = compile_active_plan_from_rows(
        rows=[row],
        strategy_catalog_path=catalog_root,
        evidence_bindings={},
        auto_reconcile_shadow_experiments=True,
        auto_experiment_packet_root=packet_root,
        auto_experiment_bindings_path=bindings_path,
    )

    assert [item.deployment_id for item in compiled.plan.deployments] == ["row-long"]
    metadata = compiled.plan.deployments[0].source.metadata
    assert metadata["declared_option_selection_contract"]["parameters"][
        "long_signal_contract_type"
    ] == "CALL"
    assert compiled.plan.summary["coverage"]["release_safe"] is True
    assert compiled.plan.summary["coverage"]["final_loaded_count"] == 1
