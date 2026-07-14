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
)


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

    def _fake_compile(**kwargs):
        assert kwargs["spreadsheet_id"] == "spreadsheet123"
        assert kwargs["catalog_sheet_name"] == "Mala_Evidence_v1"
        assert kwargs["defaults_sheet_name"] == "Operator_Defaults_v1"
        assert kwargs["strategy_sheet_name"] == "active_strategy"
        assert kwargs["manual_sheet_name"] == "manual_entry"
        return _compiled_plan("active_plan_2026-04-09")

    monkeypatch.setattr("bhiksha.tools.sync_active_plan.compile_active_plan_from_google_sheets", _fake_compile)

    exit_code = sync_active_plan_main([])

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-09"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["spy_lane"]
    log_files = sorted(log_dir.glob("active_plan_sync_*.jsonl"))
    assert len(log_files) == 1
    log_entry = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert log_entry["status"] == "ok"
    assert log_entry["summary"]["deployment_count"] == 1
    assert log_entry["suppressed"] == []


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
    assert after["smh_lane"]["effective_entry_pricing_spread_fraction"] == 0.25
    assert after["smh_lane"]["effective_entry_reprice_checkpoints_seconds"] == [60, 180]


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
    ):
        assert field not in fields


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
                    "summary": {"deployment_count": 1},
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
