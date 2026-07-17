from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from bhiksha.tools import launchd_job


def test_live_watchdog_requests_fresh_plan_before_recovery_start(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def _fake_server_session_job(args, command_args, *, repo_root):
        captured["job"] = args.job
        captured["command_args"] = command_args
        captured["repo_root"] = repo_root
        return 0

    monkeypatch.setattr(launchd_job, "_server_session_job", _fake_server_session_job)
    monkeypatch.setattr(launchd_job.os, "chdir", lambda path: None)

    exit_code = launchd_job.main(
        ["live-watchdog", "--force", "--repo-root", str(tmp_path)]
    )

    assert exit_code == 0
    assert captured["job"] == "live-watchdog"
    assert captured["repo_root"] == tmp_path.resolve()
    assert captured["command_args"][:3] == [
        "ensure-running",
        "--sync-before-start",
        "--live",
    ]


def test_recovered_google_retry_warning_does_not_alert(tmp_path: Path, monkeypatch) -> None:
    payloads: list[dict] = []
    monkeypatch.setattr(
        launchd_job,
        "_run_python_module",
        lambda args, *, repo_root: subprocess.CompletedProcess(
            args, 0, stdout="RUNTIME_STATUS={}\n", stderr="Sleeping before retry 1 of 4\n"
        ),
    )
    monkeypatch.setattr(launchd_job, "_print_result", payloads.append)
    monkeypatch.setattr(
        launchd_job,
        "_send_failure_alert",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Beacon must not receive a recovered retry")
        ),
    )

    result = launchd_job._server_session_job(
        SimpleNamespace(job="live-start"),
        ["restart", "--live"],
        repo_root=tmp_path,
    )

    assert result == 0
    assert payloads[0]["status"] == "ok"
    assert "retry 1 of 4" in payloads[0]["stderr_tail"]


def test_exhausted_google_retries_still_alert(tmp_path: Path, monkeypatch) -> None:
    payloads: list[dict] = []

    class _Alert:
        def to_dict(self):
            return {"attempted": True, "ok": True}

    monkeypatch.setattr(
        launchd_job,
        "_run_python_module",
        lambda args, *, repo_root: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="HttpError 503 after retry exhaustion\n"
        ),
    )
    monkeypatch.setattr(launchd_job, "_print_result", payloads.append)
    monkeypatch.setattr(launchd_job, "_send_failure_alert", lambda *args, **kwargs: _Alert())

    result = launchd_job._server_session_job(
        SimpleNamespace(job="live-start"),
        ["restart", "--live"],
        repo_root=tmp_path,
    )

    assert result == 2
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["alert"]["attempted"] is True
