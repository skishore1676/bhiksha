from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from bhiksha.ops.alerts import AlertResult
from bhiksha.ops.daily_report import DailyReportWriteResult
from bhiksha.tools import launchd_job


def test_report_warning_without_attention_uses_normal_receipt_level() -> None:
    assert launchd_job._alert_level_for_report(
        {
            "status": {
                "level": "YELLOW",
                "reason": "degraded_reconciliation",
                "attention_required": False,
            }
        }
    ) == "info"


def test_report_attention_uses_error_level() -> None:
    assert launchd_job._alert_level_for_report(
        {
            "status": {
                "level": "RED",
                "reason": "reconciliation_recovery_exhausted",
                "attention_required": True,
            }
        }
    ) == "error"


def test_session_report_keeps_green_domain_when_delivery_retries_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    payloads: list[dict] = []
    pid_path = tmp_path / "runtime" / "bhiksha.pid"
    runtime_status = {
        "action": "status",
        "running": True,
        "live": True,
        "pid": 4321,
        "pid_path": str(pid_path),
    }
    observed_pid_paths: list[Path] = []
    report_result = DailyReportWriteResult(
        report={
            "trading_date": "2026-08-12",
            "status": {"level": "GREEN", "reason": "ok", "attention_required": False},
        },
        json_path=tmp_path / "report.json",
        markdown_path=tmp_path / "report.md",
    )
    runtime = SimpleNamespace(
        app_config=SimpleNamespace(
            sqlite_path=str(tmp_path / "bhiksha.db"),
            playbook_artifacts_dir=str(tmp_path / "artifacts"),
        ),
        deployments=[],
    )

    monkeypatch.setenv("BHIKSHA_RUNTIME_PID_PATH", str(pid_path))
    monkeypatch.setattr(launchd_job, "build_runtime", lambda **kwargs: runtime)
    monkeypatch.setattr(
        "bhiksha.tools.server_session._runtime_status",
        lambda path: observed_pid_paths.append(path) or runtime_status,
    )
    monkeypatch.setattr(launchd_job, "write_daily_report", lambda *args, **kwargs: report_result)
    monkeypatch.setattr(launchd_job, "render_daily_report_ryg_telegram_html", lambda *args, **kwargs: "GREEN")
    monkeypatch.setattr(
        launchd_job,
        "send_lathi_alert",
        lambda **kwargs: AlertResult(
            attempted=True,
            ok=False,
            mode="live",
            transport_status="degraded",
            attempt_count=3,
            max_attempts=3,
            retry_exhausted=True,
            failure_stage="transport_timeout",
        ),
    )
    monkeypatch.setattr(launchd_job, "_publish_session_report_review", lambda *args: None)
    monkeypatch.setattr(launchd_job, "_print_result", payloads.append)

    result = launchd_job._session_report_job(
        SimpleNamespace(
            job="session-report",
            report_label="close",
            active_plan="active-plan.json",
            alert_mode="live",
            alert_profile="bhiksha-northstar",
        )
    )

    assert result == 0
    assert observed_pid_paths == [pid_path]
    assert payloads[0]["status"] == "ok"
    assert payloads[0]["report_status"]["level"] == "GREEN"
    assert payloads[0]["transport_status"] == "degraded"
    assert payloads[0]["alert"]["retry_exhausted"] is True
    assert payloads[0]["app_status"] == runtime_status


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
