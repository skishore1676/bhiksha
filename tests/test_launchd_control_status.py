from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import subprocess

import pytest

from bhiksha.ops.launchd_registry import control_lock_dir, latest_status_path
from bhiksha.ops.launchd_status_store import write_latest_status
from bhiksha.tools import launchd_control, launchd_job, launchd_status


def test_launchd_status_distinguishes_domain_and_transport(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "bhiksha.launchd.latest_status.v1",
        "generated_at": "2026-06-30T15:00:00+00:00",
        "jobs": {
            "session-report": {
                "recorded_at": "2026-06-30T15:00:00+00:00",
                "label": "com.bhiksha.session-report",
                "payload": {
                    "job": "session-report",
                    "status": "ok",
                    "report_status": {"level": "GREEN", "reason": "ok"},
                    "alert": {
                        "attempted": True,
                        "ok": False,
                        "mode": "live",
                        "return_code": 1,
                        "network_call_performed": False,
                    },
                },
            }
        },
    }
    latest_status_path(tmp_path).parent.mkdir(parents=True)
    latest_status_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": True}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
    )
    session_job = next(job for job in snapshot["jobs"] if job["runner_job"] == "session-report")

    assert session_job["last"]["domain"]["ok"] is True
    assert session_job["last"]["transport"]["status"] == "degraded"
    assert session_job["last_run_status"] == "GREEN"
    assert session_job["last_run_at"] == "2026-06-30T15:00:00+00:00"
    assert session_job["transport_status"] == "degraded"
    assert session_job["next_fire"].startswith("2026-06-30T11:45:00")
    assert snapshot["transport"]["status"] == "degraded"


def test_yellow_session_report_without_operator_gate_stays_out_of_attention(monkeypatch, tmp_path) -> None:
    write_latest_status(
        tmp_path,
        {
            "job": "session-report",
            "status": "ok",
            "report_status": {"level": "YELLOW", "reason": "provider_warning"},
        },
    )
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 18, 13, 0, tzinfo=UTC),
    )
    report = next(job for job in snapshot["jobs"] if job["runner_job"] == "session-report")

    assert report["last"]["domain"]["attention_required"] is False
    assert report["last"]["domain"]["ok"] is True
    assert report["last_run_status"] == "YELLOW"
    assert report["findings"] == []


def test_recovered_provider_warning_clears_historical_report(monkeypatch, tmp_path) -> None:
    write_latest_status(
        tmp_path,
        {
            "job": "session-report",
            "status": "ok",
            "report_status": {"level": "YELLOW", "reason": "provider_warning"},
        },
    )
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False}},
    )
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status.inspect_provider_reconciliation",
        lambda path: {
            "state": "recovered",
            "attention_required": False,
            "last_recovery": {"created_at": "2026-07-17T14:11:26+00:00"},
        },
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 18, 13, 0, tzinfo=UTC),
    )
    report = next(job for job in snapshot["jobs"] if job["runner_job"] == "session-report")

    assert report["last_run_status"] == "recovered"
    assert report["last"]["domain"]["reported_status"] == "YELLOW"
    assert report["last"]["domain"]["attention_required"] is False
    assert report["findings"] == []


def test_launchd_status_projects_stale_reconciliation_as_waiting_for_operator(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "bhiksha.launchd.latest_status.v1",
        "generated_at": "2026-07-16T15:30:00+00:00",
        "jobs": {
            "reconciliation-supervisor": {
                "recorded_at": "2026-07-16T15:30:00+00:00",
                "label": "com.bhiksha.reconciliation-supervisor",
                "payload": {
                    "job": "reconciliation-supervisor",
                    "status": "attention_required",
                    "reconciliation_supervision": {
                        "state": "needs_human",
                        "observed_at": "2026-07-16T15:30:00+00:00",
                        "attention_required": True,
                        "needs_human_count": 1,
                        "self_healing_count": 0,
                        "active_holds": [
                            {
                                "symbol": "AMD",
                                "deployment_id": "amd_short_live",
                                "entry_order_id": "PUBLIC-AMD-ORDER",
                                "state": "needs_human",
                            }
                        ],
                    },
                },
            }
        },
    }
    latest_status_path(tmp_path).parent.mkdir(parents=True)
    latest_status_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": True}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 16, 15, 31, tzinfo=UTC),
    )
    job = next(item for item in snapshot["jobs"] if item["runner_job"] == "reconciliation-supervisor")

    assert job["title"] == "Reconciliation supervision"
    assert job["lifecycle"] == "waiting_you"
    assert job["last_run_status"] == "needs_human"
    assert job["findings"] == [
        "Entry reconciliation could not finish safely; the affected deployment remains blocked."
    ]
    assert job["details"][0]["title"] == "AMD entry reconciliation"
    assert job["summary"].startswith("Needs you: 1 entry reconciliation")


def test_launchd_status_arms_failed_start_after_later_watchdog_recovery(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "bhiksha.launchd.latest_status.v1",
        "generated_at": "2026-07-14T17:50:00+00:00",
        "jobs": {
            "live-start": {
                "recorded_at": "2026-07-14T13:20:15+00:00",
                "label": "com.bhiksha.live-start",
                "payload": {
                    "job": "live-start",
                    "status": "failed",
                    "stderr_tail": "RuntimeError: Startup health check failed for: schwab_token",
                },
            },
            "live-watchdog": {
                "recorded_at": "2026-07-14T17:50:01+00:00",
                "label": "com.bhiksha.live-watchdog",
                "payload": {
                    "job": "live-watchdog",
                    "status": "ok",
                    "stdout_tail": (
                        'RUNTIME_STATUS={"running": true, "live": true, "pid": 36355, '
                        '"started_at": "2026-07-14T17:40:01+00:00"}'
                    ),
                },
            },
        },
    }
    latest_status_path(tmp_path).parent.mkdir(parents=True)
    latest_status_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {
            "ok": True,
            "status": {
                "running": True,
                "live": True,
                "pid": 36355,
                "started_at": "2026-07-14T17:40:01+00:00",
            },
        },
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 14, 17, 55, tzinfo=UTC),
    )
    live_start = next(job for job in snapshot["jobs"] if job["runner_job"] == "live-start")

    assert live_start["lifecycle"] == "armed"
    assert live_start["findings"] == []
    assert live_start["summary"] == "Recovered by live watchdog; the live runtime is running."
    assert live_start["last_run_status"] == "failed"
    assert live_start["last_run_at"] == "2026-07-14T13:20:15+00:00"
    assert live_start["last"]["domain"]["ok"] is False
    assert live_start["details"] == [
        {
            "kind": "runtime_recovery",
            "title": "Recovered by live watchdog",
            "surface": "Bhiksha live runtime",
            "status": "running; prior start failed: Startup health check failed for: schwab_token",
            "updated_at": "2026-07-14T17:50:01+00:00",
            "review_ref": "scheduled start failed at 2026-07-14T13:20:15+00:00",
        }
    ]


def test_launchd_status_preserves_recovery_after_clean_session_stop(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "bhiksha.launchd.latest_status.v1",
        "generated_at": "2026-07-14T20:10:04+00:00",
        "jobs": {
            "live-start": {
                "recorded_at": "2026-07-14T13:20:15+00:00",
                "payload": {
                    "job": "live-start",
                    "status": "failed",
                    "stderr_tail": "RuntimeError: Startup health check failed for: schwab_token",
                },
            },
            "live-watchdog": {
                "recorded_at": "2026-07-14T20:00:03+00:00",
                "payload": {
                    "job": "live-watchdog",
                    "status": "ok",
                    "stdout_tail": (
                        'RUNTIME_STATUS={"running": true, "live": true, "pid": 36355, '
                        '"started_at": "2026-07-14T17:40:01+00:00"}'
                    ),
                },
            },
            "live-stop": {
                "recorded_at": "2026-07-14T20:10:03+00:00",
                "payload": {
                    "job": "live-stop",
                    "status": "ok",
                    "stdout_tail": 'RUNTIME_STATUS={"action": "stopped", "running": false, "pid": 36355}',
                },
            },
        },
    }
    latest_status_path(tmp_path).parent.mkdir(parents=True)
    latest_status_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False, "detail": "missing_pid_file"}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 14, 20, 15, tzinfo=UTC),
    )
    live_start = next(job for job in snapshot["jobs"] if job["runner_job"] == "live-start")

    assert live_start["lifecycle"] == "armed"
    assert live_start["findings"] == []
    assert live_start["last_run_status"] == "failed"
    assert live_start["summary"] == (
        "Recovered by live watchdog; the live runtime later stopped cleanly at session close."
    )
    assert live_start["details"][0]["status"] == (
        "stopped_cleanly; prior start failed: Startup health check failed for: schwab_token"
    )
    assert live_start["details"][0]["updated_at"] == "2026-07-14T20:10:03+00:00"


@pytest.mark.parametrize(
    ("stop_status", "stop_pid", "stop_at"),
    [
        ("failed", 36355, "2026-07-14T20:10:03+00:00"),
        ("ok", 99999, "2026-07-14T20:10:03+00:00"),
        ("ok", 36355, "2026-07-14T19:59:59+00:00"),
    ],
)
def test_stopped_runtime_does_not_hide_failed_start_without_matching_clean_stop(
    stop_status, stop_pid, stop_at
) -> None:
    jobs = [
        {
            "runner_job": "live-start",
            "last_run_at": "2026-07-14T13:20:15+00:00",
            "last": {"domain": {"ok": False}, "payload": {"status": "failed"}},
            "findings": ["Domain health failed: failed"],
            "lifecycle": None,
        },
        {
            "runner_job": "live-watchdog",
            "last_run_at": "2026-07-14T20:00:03+00:00",
            "last": {
                "domain": {"ok": True},
                "payload": {
                    "stdout_tail": (
                        'RUNTIME_STATUS={"running": true, "live": true, "pid": 36355, '
                        '"started_at": "2026-07-14T17:40:01+00:00"}'
                    )
                },
            },
        },
        {
            "runner_job": "live-stop",
            "last_run_at": stop_at,
            "last": {
                "domain": {"ok": stop_status == "ok"},
                "payload": {
                    "stdout_tail": (
                        f'RUNTIME_STATUS={{"action": "stopped", "running": false, "pid": {stop_pid}}}'
                    )
                },
            },
        },
    ]

    launchd_status._apply_live_start_recovery(jobs, {"ok": True, "status": {"running": False}})

    assert jobs[0]["lifecycle"] is None
    assert jobs[0]["findings"] == ["Domain health failed: failed"]


def test_launchd_status_keeps_failed_start_stuck_without_later_watchdog_recovery(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "bhiksha.launchd.latest_status.v1",
        "generated_at": "2026-07-14T13:25:00+00:00",
        "jobs": {
            "live-start": {
                "recorded_at": "2026-07-14T13:20:15+00:00",
                "label": "com.bhiksha.live-start",
                "payload": {"job": "live-start", "status": "failed"},
            },
            "live-watchdog": {
                "recorded_at": "2026-07-14T13:30:01+00:00",
                "label": "com.bhiksha.live-watchdog",
                "payload": {
                    "job": "live-watchdog",
                    "status": "ok",
                    "stdout_tail": (
                        'RUNTIME_STATUS={"running": true, "live": true, "pid": 36355, '
                        '"started_at": "2026-07-14T13:40:00+00:00"}'
                    ),
                },
            },
        },
    }
    latest_status_path(tmp_path).parent.mkdir(parents=True)
    latest_status_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {
            "ok": True,
            "status": {
                "running": True,
                "live": True,
                "pid": 36355,
                "started_at": "2026-07-14T13:40:00+00:00",
            },
        },
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 14, 13, 25, tzinfo=UTC),
    )
    live_start = next(job for job in snapshot["jobs"] if job["runner_job"] == "live-start")

    assert live_start["lifecycle"] is None
    assert live_start["findings"] == ["Domain health failed: failed"]
    assert "summary" not in live_start
    assert "details" not in live_start


@pytest.mark.parametrize(
    "runtime_status",
    [
        {
            "ok": False,
            "status": {
                "running": True,
                "live": True,
                "pid": 36355,
                "started_at": "2026-07-14T17:40:01+00:00",
            },
        },
        {
            "ok": True,
            "status": {
                "running": True,
                "live": False,
                "pid": 36355,
                "started_at": "2026-07-14T17:40:01+00:00",
            },
        },
        {
            "ok": True,
            "status": {
                "running": True,
                "pid": 36355,
                "started_at": "2026-07-14T17:40:01+00:00",
            },
        },
    ],
)
def test_live_start_recovery_requires_successful_explicit_live_runtime_probe(runtime_status) -> None:
    jobs = [
        {
            "runner_job": "live-start",
            "last_run_at": "2026-07-14T13:20:15+00:00",
            "last_run_status": "failed",
            "last": {
                "domain": {"ok": False, "status": "failed"},
                "payload": {"job": "live-start", "status": "failed"},
            },
            "findings": ["Domain health failed: failed"],
            "lifecycle": None,
        },
        {
            "runner_job": "live-watchdog",
            "label": "com.bhiksha.live-watchdog",
            "last_run_at": "2026-07-14T17:50:01+00:00",
            "last": {
                "domain": {"ok": True, "status": "ok"},
                "payload": {
                    "stdout_tail": (
                        'RUNTIME_STATUS={"running": true, "live": true, "pid": 36355, '
                        '"started_at": "2026-07-14T17:40:01+00:00"}'
                    )
                },
            },
        },
    ]

    launchd_status._apply_live_start_recovery(jobs, runtime_status)

    assert jobs[0]["lifecycle"] is None
    assert jobs[0]["findings"] == ["Domain health failed: failed"]


def test_runtime_status_parses_payload_and_returns_dict(monkeypatch, tmp_path) -> None:
    # Regression: _runtime_status previously fell off the end without a return
    # (its body had been misplaced after _bhiksha_python's return), so it always
    # returned None even for a healthy runtime, leaving snapshot["runtime"] None.
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0] if args else ["status"],
            0,
            stdout='RUNTIME_STATUS={"running": true, "pid": 4242}\n',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", fake_run)

    runtime = launchd_status._runtime_status(repo_root=tmp_path)

    assert runtime is not None
    assert runtime["ok"] is True
    assert runtime["return_code"] == 0
    assert runtime["status"] == {"running": True, "pid": 4242}


def test_launchd_job_writes_latest_status(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    write_latest_status(tmp_path, {"job": "session-report", "status": "ok"})

    written = json.loads(latest_status_path(tmp_path).read_text(encoding="utf-8"))
    assert written["schema"] == "bhiksha.launchd.latest_status.v1"
    assert written["jobs"]["session-report"]["label"] == "com.bhiksha.session-report"
    assert written["jobs"]["session-report"]["payload"]["status"] == "ok"


def test_weekly_preview_cannot_replace_passing_report(tmp_path) -> None:
    artifacts = tmp_path / "artifacts" / "playbook"

    assert launchd_job._weekly_report_output_dir(
        artifacts,
        workbook_update_mode="on",
    ) == artifacts / "reports"
    assert launchd_job._weekly_report_output_dir(
        artifacts,
        workbook_update_mode="off",
    ) == artifacts / "reports" / "previews"


def test_launchd_control_live_status(monkeypatch, tmp_path) -> None:
    def fake_status(repo_root):
        return subprocess.CompletedProcess(
            ["status"],
            0,
            stdout='RUNTIME_STATUS={"running": true, "pid": 123}\n',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.tools.launchd_control._run_server_session_status", fake_status)

    result = launchd_control.run_control_action(
        action="live-status",
        repo_root=tmp_path,
        action_id="act-live-status",
    )

    assert result["ok"] is True
    assert result["runtime"]["running"] is True
    assert result["action_id"] == "act-live-status"


def test_launchd_control_requires_confirmation_for_live_ensure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "bhiksha.tools.launchd_control._confirmation_requirement",
        lambda action, repo_root: {"required": True, "reasons": ["would_start_stopped_live_runtime"]},
    )

    result = launchd_control.run_control_action(
        action="ensure-live-runtime",
        repo_root=tmp_path,
        action_id="act-ensure",
        confirmed=False,
    )

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert result["reason"] == "confirmation_required"


def test_launchd_control_requires_confirmation_for_schwab_renewal(tmp_path) -> None:
    result = launchd_control.run_control_action(
        action="renew-schwab-access",
        repo_root=tmp_path,
        action_id="act-renew",
        confirmed=False,
    )

    assert result["ok"] is False
    assert result["reason"] == "confirmation_required"
    assert result["confirmation"]["reasons"] == ["grants_schwab_account_access"]


def test_launchd_control_confirmed_schwab_renewal_forces_browser(monkeypatch, tmp_path) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='BHIKSHA_LAUNCHD_JOB={"job": "schwab-refresh", "status": "ok"}\n',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.tools.launchd_control._run_control_command", fake_run)

    result = launchd_control.run_control_action(
        action="renew-schwab-access",
        repo_root=tmp_path,
        action_id="act-renew",
        confirmed=True,
    )

    assert result["ok"] is True
    assert result["confirmed"] is True
    assert result["command"][1:5] == ["schwab-refresh", "--force", "--browser-renewal-mode", "force"]


def test_launchd_control_schwab_check_never_starts_browser(monkeypatch, tmp_path) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='BHIKSHA_LAUNCHD_JOB={"status":"ok"}\n', stderr="")

    monkeypatch.setattr("bhiksha.tools.launchd_control._run_control_command", fake_run)

    result = launchd_control.run_control_action(
        action="schwab-guard-now",
        repo_root=tmp_path,
        action_id="act-check",
    )

    assert result["ok"] is True
    assert result["command"][1:5] == ["schwab-refresh", "--force", "--browser-renewal-mode", "off"]


def test_launchd_status_surfaces_schwab_auth_failure_and_confirmed_action(monkeypatch, tmp_path) -> None:
    write_latest_status(
        tmp_path,
        {
            "job": "schwab-refresh",
            "status": "failed",
            "result": {
                "ok": False,
                "attention_required": True,
                "failure_kind": "schwab_authentication_expired",
                "final": {"state": "refresh_token_expired"},
            },
        },
    )
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 14, 16, 0, tzinfo=UTC),
    )
    guard = next(job for job in snapshot["jobs"] if job["runner_job"] == "schwab-refresh")

    assert guard["title"] == "Schwab authentication"
    assert guard["lifecycle"] == "waiting_you"
    assert guard["findings"] == ["Schwab authentication expired; renewal is required."]
    assert "renew-schwab-access" in guard["available_actions"]
    assert guard["action_requirements"]["renew-schwab-access"]["requires_confirmation"] is True
    assert guard["action_requirements"]["renew-schwab-access"]["owner_confirmation_args"] == ["--confirm"]


def test_launchd_status_treats_non_trading_day_schwab_skip_as_healthy(monkeypatch, tmp_path) -> None:
    write_latest_status(
        tmp_path,
        {"job": "schwab-refresh", "status": "skipped", "reason": "non_trading_day"},
    )
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 3, 13, 0, tzinfo=UTC),
    )
    guard = next(job for job in snapshot["jobs"] if job["runner_job"] == "schwab-refresh")

    assert guard["last"]["domain"] == {"ok": True, "status": "skipped", "reason": "non_trading_day"}
    assert guard["findings"] == []


def test_non_trading_day_skip_does_not_erase_unresolved_schwab_failure(monkeypatch, tmp_path) -> None:
    failed = {
        "job": "schwab-refresh",
        "status": "failed",
        "result": {
            "ok": False,
            "attention_required": True,
            "failure_kind": "browser_renewal_failed",
            "final": {"state": "refresh_token_near_expiry"},
        },
    }
    write_latest_status(tmp_path, failed)
    write_latest_status(tmp_path, {"job": "schwab-refresh", "status": "skipped", "reason": "non_trading_day"})
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda **kwargs: {})
    monkeypatch.setattr(
        "bhiksha.tools.launchd_status._runtime_status",
        lambda *, repo_root, **kwargs: {"ok": True, "status": {"running": False}},
    )

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 3, 13, 0, tzinfo=UTC),
    )
    guard = next(job for job in snapshot["jobs"] if job["runner_job"] == "schwab-refresh")
    stored = json.loads(latest_status_path(tmp_path).read_text(encoding="utf-8"))["jobs"]["schwab-refresh"]

    assert guard["lifecycle"] == "waiting_you"
    assert guard["findings"] == ["Automatic Schwab authentication renewal failed."]
    assert stored["payload"]["status"] == "failed"
    assert stored["last_skip_payload"]["status"] == "skipped"


def test_launchd_control_refuses_duplicate_action(tmp_path) -> None:
    locks = control_lock_dir(tmp_path)
    locks.mkdir(parents=True)
    lock = locks / "session-report-now.lock"
    lock.write_text(
        json.dumps(
            {
                "action": "session-report-now",
                "action_id": "existing",
                "started_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = launchd_control.run_control_action(
        action="session-report-now",
        repo_root=tmp_path,
        action_id="new",
        lock_stale_seconds=3600,
    )

    assert result["ok"] is False
    assert result["reason"] == "action_already_running"
    assert result["in_flight"]["action_id"] == "existing"


def test_launchd_control_reclaims_stale_action_lock(monkeypatch, tmp_path) -> None:
    locks = control_lock_dir(tmp_path)
    locks.mkdir(parents=True)
    lock = locks / "session-report-now.lock"
    lock.write_text(
        json.dumps(
            {
                "action": "session-report-now",
                "action_id": "old",
                "started_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='BHIKSHA_LAUNCHD_JOB={"job": "session-report", "status": "ok", "action_id": "new"}\n',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.tools.launchd_control._run_control_command", fake_run)

    result = launchd_control.run_control_action(
        action="session-report-now",
        repo_root=tmp_path,
        action_id="new",
        lock_stale_seconds=1,
    )

    assert result["ok"] is True
    assert result["bhiksha_job"]["action_id"] == "new"
    assert "--action-id" in result["command"]


def test_runtime_status_parses_runtime_status_line(monkeypatch, tmp_path) -> None:
    """Operator-audit regression (2026-07-02): _runtime_status's parsing block
    was stranded after _bhiksha_python's return (dead code), so the function
    silently returned None and Control Tower reported no runtime state at all.
    When server_session status emits RUNTIME_STATUS=..., the parsed payload
    must be non-null."""
    from bhiksha.tools import launchd_status

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='before\nRUNTIME_STATUS={"running": true, "active_plan_id": "active_plan_2026-07-02"}\n',
            stderr="",
        )

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", fake_run)

    result = launchd_status._runtime_status(repo_root=tmp_path)

    assert result is not None, "stranded-return regression: _runtime_status returned None"
    assert result["ok"] is True
    assert result["return_code"] == 0
    assert result["status"] == {"running": True, "active_plan_id": "active_plan_2026-07-02"}


def test_runtime_status_handles_missing_runtime_line(monkeypatch, tmp_path) -> None:
    from bhiksha.tools import launchd_status

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=3, stdout="no marker here\n", stderr="boom")

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", fake_run)

    result = launchd_status._runtime_status(repo_root=tmp_path)

    assert result["ok"] is False
    assert result["return_code"] == 3
    assert result["status"] is None


def test_launchd_state_probe_timeout_degrades_only_that_field(monkeypatch) -> None:
    """One slow `launchctl print` must degrade only that job's launchd field to
    an explicit "timeout" value; the other probes and the overall snapshot must
    proceed normally (external callers kill the whole command at 20s)."""
    from bhiksha.ops.launchd_registry import active_launchd_jobs

    labels = [spec.label for spec in active_launchd_jobs()]
    slow_label = labels[0]

    monkeypatch.setattr("bhiksha.tools.launchd_status.shutil.which", lambda name: "/bin/launchctl")

    def fake_run(command, **kwargs):
        if slow_label in command[-1]:
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(
            command, 0, stdout="state = running\nlast exit code = 0\n", stderr=""
        )

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", fake_run)

    state = launchd_status._launchd_state(deadline=launchd_status._Deadline(15.0))

    assert state[slow_label]["state"] == "timeout"
    assert state[slow_label]["loaded"] is None
    assert state[slow_label]["available"] is True
    for label in labels[1:]:
        assert state[label]["loaded"] is True
        assert state[label]["state"] == "running"


def test_status_snapshot_survives_all_probes_timing_out(monkeypatch, tmp_path) -> None:
    """TimeoutExpired from any subprocess probe (launchctl or server_session)
    must never propagate; the snapshot stays valid same-schema JSON with
    degraded field values."""
    monkeypatch.setattr("bhiksha.tools.launchd_status.shutil.which", lambda name: "/bin/launchctl")

    def always_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", always_timeout)

    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 5, 15, 0, tzinfo=UTC),
        budget_seconds=15.0,
    )

    round_tripped = json.loads(json.dumps(snapshot, default=str))
    assert round_tripped["schema"] == "bhiksha.launchd.status.v1"
    for job in round_tripped["jobs"]:
        assert job["launchd"]["state"] == "timeout"
        assert job["launchd"]["loaded"] is None
    assert round_tripped["runtime"]["ok"] is False
    assert round_tripped["runtime"]["return_code"] is None
    assert round_tripped["runtime"]["stderr_tail"].startswith("timeout:")


def test_status_snapshot_deadline_exhaustion_short_circuits_to_not_checked(monkeypatch, tmp_path) -> None:
    """With the overall budget exhausted, remaining probes must not run at all:
    they short-circuit to explicit "not_checked" values and the snapshot
    returns immediately as valid JSON."""
    import time as _time

    monkeypatch.setattr("bhiksha.tools.launchd_status.shutil.which", lambda name: "/bin/launchctl")

    def must_not_run(command, **kwargs):
        raise AssertionError("subprocess.run must not be called once the budget is exhausted")

    monkeypatch.setattr("bhiksha.tools.launchd_status.subprocess.run", must_not_run)

    started = _time.monotonic()
    snapshot = launchd_status.build_status_snapshot(
        repo_root=tmp_path,
        active_plan_path=tmp_path / "active_plan.json",
        now=datetime(2026, 7, 5, 15, 0, tzinfo=UTC),
        budget_seconds=0.0,
    )
    elapsed = _time.monotonic() - started

    assert elapsed < 2.0, f"snapshot took {elapsed:.2f}s despite exhausted budget"
    round_tripped = json.loads(json.dumps(snapshot, default=str))
    for job in round_tripped["jobs"]:
        assert job["launchd"]["state"] == "not_checked"
        assert job["launchd"]["loaded"] is None
    assert round_tripped["runtime"]["ok"] is False
    assert round_tripped["runtime"]["stderr_tail"].startswith("not_checked:")
