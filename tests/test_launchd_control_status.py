from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import subprocess

from bhiksha.ops.launchd_registry import control_lock_dir, latest_status_path
from bhiksha.ops.launchd_status_store import write_latest_status
from bhiksha.tools import launchd_control, launchd_status


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
    monkeypatch.setattr("bhiksha.tools.launchd_status._launchd_state", lambda: {})
    monkeypatch.setattr("bhiksha.tools.launchd_status._runtime_status", lambda repo_root: {"ok": True, "status": {"running": True}})

    snapshot = launchd_status.build_status_snapshot(repo_root=tmp_path, active_plan_path=tmp_path / "active_plan.json")
    session_job = next(job for job in snapshot["jobs"] if job["runner_job"] == "session-report")

    assert session_job["last"]["domain"]["ok"] is True
    assert session_job["last"]["transport"]["status"] == "degraded"
    assert snapshot["transport"]["status"] == "degraded"


def test_launchd_job_writes_latest_status(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    write_latest_status(tmp_path, {"job": "session-report", "status": "ok"})

    written = json.loads(latest_status_path(tmp_path).read_text(encoding="utf-8"))
    assert written["schema"] == "bhiksha.launchd.latest_status.v1"
    assert written["jobs"]["session-report"]["label"] == "com.bhiksha.session-report"
    assert written["jobs"]["session-report"]["payload"]["status"] == "ok"


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

    monkeypatch.setattr("bhiksha.tools.launchd_control.subprocess.run", fake_run)

    result = launchd_control.run_control_action(
        action="session-report-now",
        repo_root=tmp_path,
        action_id="new",
        lock_stale_seconds=1,
    )

    assert result["ok"] is True
    assert result["bhiksha_job"]["action_id"] == "new"
    assert "--action-id" in result["command"]
