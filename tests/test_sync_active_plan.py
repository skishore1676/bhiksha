from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.config.loader import load_active_plan
from bhiksha.tools.sync_active_plan import (
    _write_if_changed,
    diff_lane_configs,
    lane_config_snapshot,
    main as sync_active_plan_main,
    sync_active_plan_once,
)
from bhiksha.tools.compile_active_plan import main as compile_active_plan_main


def test_sync_active_plan_uses_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "artifacts" / "playbook" / "active_plan.json"
    log_dir = tmp_path / "artifacts" / "playbook" / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("MALA_EVIDENCE_SHEET_NAME", "Mala_Evidence_v1")
    monkeypatch.setenv("ACTIVE_STRATEGIES_SHEET_NAME", "active_strategy")
    monkeypatch.setenv("MANUAL_ENTRY_SHEET_NAME", "manual_entry")
    monkeypatch.setenv("BHIKSHA_STRATEGY_CATALOG_PATH", str(tmp_path / "strategy_catalog"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_PATH", str(output_path))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(log_dir))
    monkeypatch.setenv(
        "BHIKSHA_ACTIVE_PLAN_ID",
        "active_plan_2026-07-27_exit_engine_v2_iwm_canary",
    )

    def _fake_compile(**kwargs):
        assert kwargs["spreadsheet_id"] == "spreadsheet123"
        assert kwargs["catalog_sheet_name"] == "Mala_Evidence_v1"
        assert kwargs["defaults_sheet_name"] == "Operator_Defaults_v1"
        assert kwargs["strategy_sheet_name"] == "active_strategy"
        assert kwargs["manual_sheet_name"] == "manual_entry"
        assert kwargs["active_plan_id"] == (
            "active_plan_2026-07-27_exit_engine_v2_iwm_canary"
        )
        return _compiled_plan(kwargs["active_plan_id"])

    monkeypatch.setattr("bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets", _fake_compile)

    exit_code = sync_active_plan_main([])

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == (
        "active_plan_2026-07-27_exit_engine_v2_iwm_canary"
    )
    assert [deployment.deployment_id for deployment in plan.deployments] == ["spy_lane"]
    log_files = sorted(log_dir.glob("active_plan_sync_*.jsonl"))
    assert len(log_files) == 1
    log_entry = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert log_entry["status"] == "ok"
    assert log_entry["summary"]["deployment_count"] == 1
    assert log_entry["suppressed"] == []


def test_sync_active_plan_explicit_id_overrides_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "active_plan.json"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv(
        "GOOGLE_API_CREDENTIALS_PATH",
        str(tmp_path / "credentials.json"),
    )
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_ID", "env-plan")

    def _fake_compile(**kwargs):
        assert kwargs["active_plan_id"] == "cli-plan"
        return _compiled_plan(kwargs["active_plan_id"])

    monkeypatch.setattr(
        "bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets",
        _fake_compile,
    )

    assert sync_active_plan_main(
        [
            "--out",
            str(output_path),
            "--active-plan-id",
            "cli-plan",
        ]
    ) == 0
    assert load_active_plan(output_path).active_plan_id == "cli-plan"


def test_sync_active_plan_blank_environment_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", "credentials.json")
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_ID", "   ")

    with pytest.raises(ValueError, match="nonblank stable active-plan id"):
        sync_active_plan_main([])


def test_sync_active_plan_repeats_without_rewriting_when_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "active_plan.json"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(log_dir))

    calls: list[int] = []
    sleeps: list[float] = []

    def _fake_compile(**kwargs):
        del kwargs
        calls.append(1)
        return _compiled_plan("active_plan_2026-04-09")

    monkeypatch.setattr("bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets", _fake_compile)
    monkeypatch.setattr("bhiksha.tools.sync_active_plan.time.sleep", lambda seconds: sleeps.append(seconds))

    exit_code = sync_active_plan_main(
        [
            "--out",
            str(output_path),
            "--interval-minutes",
            "20",
            "--iterations",
            "2",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 2
    assert sleeps == [1200.0]
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-09"
    first_contents = output_path.read_text(encoding="utf-8")
    second_contents = output_path.read_text(encoding="utf-8")
    assert first_contents == second_contents
    log_files = sorted(log_dir.glob("active_plan_sync_*.jsonl"))
    assert len(log_files) == 1
    assert len(log_files[0].read_text(encoding="utf-8").splitlines()) == 2


def test_sync_active_plan_records_lane_config_and_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_path = tmp_path / "active_plan.json"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(log_dir))

    previous_plan = {
        "deployments": [
            {
                "deployment_id": "spy_lane",
                "symbol": "SPY",
                "execution": {"shadow_only": True},
                "risk": {"max_trade_premium_usd": 300},
                "exit": {
                    "stop_loss_pct": 0.35,
                    "option_profit_target_pct": 0.35,
                    "use_profit_target": True,
                    "hard_flat_time_et": "15:55",
                },
            },
            {
                "deployment_id": "retired_lane",
                "symbol": "IWM",
                "execution": {"shadow_only": False},
                "risk": {},
                "exit": {},
            },
        ]
    }
    output_path.write_text(json.dumps(previous_plan), encoding="utf-8")

    monkeypatch.setattr(
        "bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets",
        lambda **kwargs: _compiled_plan("active_plan_2026-04-09"),
    )

    exit_code = sync_active_plan_main(["--out", str(output_path)])

    assert exit_code == 0
    log_entry = json.loads(sorted(log_dir.glob("active_plan_sync_*.jsonl"))[0].read_text(encoding="utf-8").splitlines()[0])
    assert log_entry["lane_config"]["spy_lane"]["stop_loss_pct"] == 0.45
    changes = {change["deployment_id"]: change for change in log_entry["lane_config_changes"]}
    assert changes["retired_lane"]["change"] == "removed"
    spy_fields = changes["spy_lane"]["fields"]
    assert spy_fields["stop_loss_pct"] == {"before": 0.35, "after": 0.45}
    assert spy_fields["option_profit_target_pct"] == {"before": 0.35, "after": None}
    assert spy_fields["use_profit_target"] == {"before": True, "after": False}


def test_lane_config_diff_reports_added_and_unchanged_lanes() -> None:
    before = {"a": {"symbol": "SPY", "stop_loss_pct": 0.35}}
    after = {
        "a": {"symbol": "SPY", "stop_loss_pct": 0.35},
        "b": {"symbol": "QQQ", "stop_loss_pct": 0.45},
    }
    changes = diff_lane_configs(before, after)
    assert changes == [{"deployment_id": "b", "change": "added", "config": after["b"]}]


def test_lane_config_snapshot_skips_malformed_deployments() -> None:
    plan = {
        "deployments": [
            {"deployment_id": "ok_lane", "symbol": "SPY", "exit": {"stop_loss_pct": 0.4}},
            {"symbol": "missing_id"},
            "not_a_dict",
        ]
    }
    snapshot = lane_config_snapshot(plan)
    assert set(snapshot) == {"ok_lane"}
    assert snapshot["ok_lane"]["stop_loss_pct"] == 0.4
    assert snapshot["ok_lane"]["max_trade_premium_usd"] is None


def test_lane_config_snapshot_receipts_all_canary_authority_fields() -> None:
    lane = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "iwm_canary",
                    "symbol": "IWM",
                    "execution": {
                        "dte_min": 4,
                        "dte_max": 7,
                        "dte_fallback_policy": "strict",
                    },
                    "risk": {
                        "max_trade_premium_usd": 2_000.0,
                        "max_contracts": 1,
                    },
                    "exit": {
                        "risk_envelope_live_mode": "canary",
                        "risk_envelope_live_candidate_id": "safety_stack",
                        "risk_envelope_live_candidate_overlay_hash": "overlay",
                        "risk_envelope_live_authorization_id": "auth",
                        "risk_envelope_live_start_at": "2026-07-20T00:00:00Z",
                        "risk_envelope_live_expires_at": "2026-08-01T00:00:00Z",
                        "risk_envelope_live_authorized_deployment_id": "iwm_canary",
                        "risk_envelope_live_authorized_symbol": "IWM",
                        "risk_envelope_live_authorized_active_plan_id": "plan",
                        "risk_envelope_live_rollback_action": (
                            "disable_canary_restore_control"
                        ),
                        "risk_envelope_live_max_premium_cap_fraction": 0.20,
                        "risk_envelope_live_max_quote_age_ms": 2_000,
                        "risk_envelope_live_max_spread_pct": 0.15,
                    },
                }
            ]
        }
    )["iwm_canary"]

    assert lane["dte_min"] == 4
    assert lane["dte_max"] == 7
    assert lane["dte_fallback_policy"] == "strict"
    assert lane["max_contracts"] == 1
    assert lane["risk_envelope_live_authorized_active_plan_id"] == "plan"
    assert lane["risk_envelope_live_rollback_action"] == (
        "disable_canary_restore_control"
    )


def test_lane_config_diff_surfaces_patient_entry_policy_changes() -> None:
    before = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "symbol": "SMH",
                    "execution": {
                        "min_open_interest": 100,
                        "max_bid_ask_spread_pct": 0.08,
                    },
                }
            ]
        }
    )
    after = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "symbol": "SMH",
                    "execution": {
                        "min_open_interest": 50,
                        "max_bid_ask_spread_pct": 0.12,
                        "entry_execution_profile": "patient",
                        "entry_pricing_spread_fraction": 0.25,
                        "entry_pricing_oi_percentile_scale": True,
                        "entry_reprice_enabled": True,
                        "entry_reprice_checkpoints_seconds": [60, 180],
                        "entry_reprice_cancel_after_seconds": 300,
                        "entry_reprice_spread_fractions": [0.50, 0.70],
                        "entry_reprice_max_chase_pct": 0.08,
                    },
                }
            ]
        }
    )

    changes = diff_lane_configs(before, after)

    assert len(changes) == 1
    assert changes[0]["deployment_id"] == "smh_lane"
    fields = changes[0]["fields"]
    assert fields["min_open_interest"] == {"before": 100, "after": 50}
    assert fields["entry_execution_profile"] == {"before": None, "after": "patient"}
    assert fields["entry_reprice_spread_fractions"] == {"before": None, "after": [0.50, 0.70]}
    assert fields["entry_reprice_max_chase_pct"] == {"before": None, "after": 0.08}
    assert after["smh_lane"]["effective_entry_pricing_spread_fraction"] == 0.25
    assert after["smh_lane"]["effective_entry_reprice_checkpoints_seconds"] == [60, 180]
    assert after["smh_lane"]["effective_entry_reprice_max_chase_pct"] == 0.08


def test_named_patient_profile_replaces_explicit_ladder_without_changing_effective_policy() -> None:
    before = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "execution": {
                        "entry_pricing_spread_fraction": 0.25,
                        "entry_pricing_oi_percentile_scale": True,
                        "entry_reprice_enabled": True,
                        "entry_reprice_checkpoints_seconds": [60, 180],
                        "entry_reprice_cancel_after_seconds": 300,
                        "entry_reprice_spread_fractions": [0.50, 0.70],
                        "entry_reprice_max_chase_pct": 0.10,
                    },
                }
            ]
        }
    )
    after = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "execution": {"entry_execution_profile": "patient"},
                }
            ]
        }
    )

    changes = diff_lane_configs(before, after)

    assert len(changes) == 1
    fields = changes[0]["fields"]
    assert fields["entry_execution_profile"] == {"before": None, "after": "patient"}
    assert fields["entry_pricing_spread_fraction"] == {"before": 0.25, "after": None}
    for field in (
        "effective_entry_pricing_spread_fraction",
        "effective_entry_pricing_oi_percentile_scale",
        "effective_entry_reprice_enabled",
        "effective_entry_reprice_checkpoints_seconds",
        "effective_entry_reprice_cancel_after_seconds",
        "effective_entry_reprice_spread_fractions",
        "effective_entry_reprice_max_chase_pct",
    ):
        assert field not in fields


def test_materialized_profile_cap_surfaces_new_runtime_semantics() -> None:
    before = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "execution": {"entry_execution_profile": "patient"},
                }
            ]
        }
    )
    after = lane_config_snapshot(
        {
            "deployments": [
                {
                    "deployment_id": "smh_lane",
                    "execution": {
                        "entry_execution_profile": "patient",
                        "entry_reprice_max_chase_pct": 0.10,
                    },
                }
            ]
        }
    )

    changes = diff_lane_configs(before, after)

    assert changes[0]["fields"]["entry_reprice_max_chase_pct"] == {
        "before": None,
        "after": 0.10,
    }
    assert "effective_entry_reprice_max_chase_pct" not in changes[0]["fields"]


def test_sync_active_plan_logs_compile_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(log_dir))

    def _fake_compile(**kwargs):
        del kwargs
        raise ValueError("sheet access failed")

    monkeypatch.setattr("bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets", _fake_compile)

    with pytest.raises(ValueError, match="sheet access failed"):
        sync_active_plan_main([])

    log_files = sorted(log_dir.glob("active_plan_sync_*.jsonl"))
    assert len(log_files) == 1
    log_entry = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert log_entry["status"] == "error"
    assert log_entry["error"] == "sheet access failed"


def test_sync_coverage_gate_preserves_previous_plan_on_unclassified_lane_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "active_plan.json"
    previous_bytes = b'{"active_plan_id":"previous","deployments":["a","b"]}\n'
    output_path.write_bytes(previous_bytes)
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "spreadsheet123")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", str(log_dir))

    candidate = _compiled_plan("degraded-candidate")
    candidate.plan.summary["coverage"] = {
        "expected_enabled_row_count": 2,
        "pre_observation_compiled_count": 1,
        "final_loaded_count": 1,
        "intentional_pre_observation_suppression_count": 0,
        "observation_binding_suppression_count": 0,
        "live_evidence_quarantine_count": 0,
        "unexpected_coverage_loss_count": 1,
        "unexpected_coverage_loss_deployment_ids": ["lost-lane"],
        "release_safe": False,
    }
    monkeypatch.setattr(
        "bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets",
        lambda **kwargs: candidate,
    )

    with pytest.raises(ValueError, match="failed coverage gate"):
        sync_active_plan_main(["--out", str(output_path)])

    assert output_path.read_bytes() == previous_bytes
    log_entry = json.loads(
        sorted(log_dir.glob("active_plan_sync_*.jsonl"))[0]
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert log_entry["status"] == "error"
    assert "previous active plan preserved" in log_entry["error"]


def test_candidate_only_cannot_target_canonical_active_plan_from_another_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_output = (
        Path(__file__).resolve().parents[1]
        / "artifacts/playbook/active_plan.json"
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        compile_active_plan_main(
            [
                "--candidate-only",
                "--google-sheet-id",
                "spreadsheet123",
                "--credentials-path",
                "credentials.json",
                "--out",
                str(canonical_output),
            ]
        )

    assert exc.value.code == 2


def test_sync_missing_canonical_packet_root_preserves_plan_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "active_plan.json"
    runtime_registry = tmp_path / "evidence_bindings_v1.json"
    output_bytes = b'{"active_plan_id":"previous"}\n'
    registry_bytes = b'{"registry":"previous"}\n'
    output_path.write_bytes(output_bytes)
    runtime_registry.write_bytes(registry_bytes)
    monkeypatch.setattr(
        "bhiksha.tools.sync_active_plan.resolve_mala_packet_root", lambda: None
    )

    with pytest.raises(ValueError, match="canonical Mala evidence packet root"):
        sync_active_plan_once(
            spreadsheet_id="spreadsheet123",
            credentials_path="credentials.json",
            catalog_sheet_name="Mala_Evidence_v1",
            defaults_sheet_name=None,
            strategy_sheet_name="active_strategy",
            manual_sheet_name="manual_entry",
            strategy_catalog_path=tmp_path / "catalog",
            output_path=output_path,
            log_dir=tmp_path / "logs",
            runtime_capabilities_path=None,
        )

    assert output_path.read_bytes() == output_bytes
    assert runtime_registry.read_bytes() == registry_bytes


def test_sync_rolls_back_promoted_packets_and_registries_when_plan_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bhiksha.evidence.bindings import build_registry_payload
    import bhiksha.tools.sync_active_plan as sync_module

    output_path = tmp_path / "active_plan.json"
    output_path.write_text('{"active_plan_id":"previous"}\n')
    runtime_registry = tmp_path / "evidence_bindings_v1.json"
    prior_runtime = json.dumps(build_registry_payload([])) + "\n"
    runtime_registry.write_text(prior_runtime)
    packet_root = tmp_path / "canonical_packets"
    packet_root.mkdir()
    packet_registry = packet_root / "registry.json"
    packet_registry.write_text('{"previous":true}\n')
    monkeypatch.setattr(sync_module, "resolve_mala_packet_root", lambda: packet_root)

    def fake_compile(**kwargs):
        staged_packet_root = Path(kwargs["auto_experiment_packet_root"])
        new_dir = staged_packet_root / "new-packet"
        new_dir.mkdir()
        (new_dir / "manifest.json").write_text(
            json.dumps({"evidence_packet_id": "a" * 64})
        )
        (staged_packet_root / "registry.json").write_text('{"changed":true}\n')
        Path(kwargs["auto_experiment_bindings_path"]).write_text(
            json.dumps(build_registry_payload([]), indent=2) + "\n"
        )
        return _compiled_plan("accepted-candidate")

    monkeypatch.setattr(sync_module, "compile_active_plan_from_google_sheets", fake_compile)
    real_write = sync_module._write_if_changed

    def fail_plan_write(path: Path, payload: str) -> bool:
        if Path(path) == output_path:
            raise OSError("simulated plan publish failure")
        return real_write(path, payload)

    monkeypatch.setattr(sync_module, "_write_if_changed", fail_plan_write)

    with pytest.raises(OSError, match="simulated plan publish failure"):
        sync_active_plan_once(
            spreadsheet_id="spreadsheet123",
            credentials_path="credentials.json",
            catalog_sheet_name="Mala_Evidence_v1",
            defaults_sheet_name=None,
            strategy_sheet_name="active_strategy",
            manual_sheet_name="manual_entry",
            strategy_catalog_path=tmp_path / "catalog",
            output_path=output_path,
            log_dir=tmp_path / "logs",
            runtime_capabilities_path=None,
            evidence_bindings_path=runtime_registry,
        )

    assert not (packet_root / "new-packet").exists()
    assert packet_registry.read_text() == '{"previous":true}\n'
    assert runtime_registry.read_text() == prior_runtime
    assert output_path.read_text() == '{"active_plan_id":"previous"}\n'


def test_write_if_changed_preserves_existing_file_when_temp_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "active_plan.json"
    output_path.write_text('{"active_plan_id": "previous"}\n', encoding="utf-8")

    class FailingTempFile:
        name = str(tmp_path / ".active_plan.json.fail.tmp")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, payload: str) -> None:
            del payload
            raise OSError("disk full")

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return 0

    monkeypatch.setattr("bhiksha.tools.sync_active_plan.tempfile.NamedTemporaryFile", lambda *args, **kwargs: FailingTempFile())

    with pytest.raises(OSError, match="disk full"):
        _write_if_changed(output_path, '{"active_plan_id": "new"}\n')

    assert output_path.read_text(encoding="utf-8") == '{"active_plan_id": "previous"}\n'


def _compiled_plan(active_plan_id: str):
    class _Compiled:
        def __init__(self):
            self.plan = load_active_plan_from_dict(
                {
                    "contract_name": "active_plan",
                    "schema_version": 1,
                    "active_plan_id": active_plan_id,
                    "trading_date": "2026-04-09",
                    "generated_at": "2026-04-09T14:00:00Z",
                    "source": {"name": "test"},
                    "summary": {
                        "deployment_count": 1,
                        "coverage": {
                            "expected_enabled_row_count": 1,
                            "pre_observation_compiled_count": 1,
                            "final_loaded_count": 1,
                            "intentional_pre_observation_suppression_count": 0,
                            "observation_binding_suppression_count": 0,
                            "live_evidence_quarantine_count": 0,
                            "unexpected_coverage_loss_count": 0,
                            "release_safe": True,
                        },
                    },
                    "suppressed": [],
                    "deployments": [
                        {
                            "deployment_id": "spy_lane",
                            "enabled": True,
                            "symbol": "SPY",
                            "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
                            "execution": {
                                "profile": "single_leg_long_premium_v1",
                                "shadow_only": True,
                                "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                                "dte_min": 0,
                                "dte_max": 7,
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
                                "profile": "market_impulse_exit_v1",
                                "use_algorithmic_exit": True,
                                "use_profit_target": False,
                                "stop_loss_pct": 0.45,
                                "hard_flat_time_et": "15:55",
                            },
                            "source": {"origin": "test"},
                        }
                    ],
                }
            )

    return _Compiled()


def load_active_plan_from_dict(payload: dict):
    path = Path("/tmp/unused_active_plan_payload.json")
    del path
    from bhiksha.config.models import ActivePlan

    return ActivePlan.model_validate(payload)
