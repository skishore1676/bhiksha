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
