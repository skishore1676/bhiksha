"""Bhiksha-owned launchd job runner."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from bhiksha.app.bootstrap import build_runtime
from bhiksha.config.environment import load_dotenv
from bhiksha.market_data.trading_calendar import is_trading_day
from bhiksha.ops.alerts import AlertMode, send_lathi_alert
from bhiksha.ops.daily_report import render_daily_report_telegram_summary, write_daily_report
from bhiksha.ops.schwab_token_guard import run_schwab_token_guard_sync

CENTRAL = ZoneInfo("America/Chicago")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job",
        choices=["live-start", "live-watchdog", "live-stop", "schwab-refresh", "session-report"],
    )
    parser.add_argument("--force", action="store_true", help="Run even when today is not a trading day")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--active-plan", default="artifacts/playbook/active_plan.json")
    parser.add_argument("--report-label", default="scheduled")
    parser.add_argument("--alert-mode", default=os.getenv("BHIKSHA_LAUNCHD_ALERT_MODE", "live"), choices=["off", "spool", "live"])
    parser.add_argument("--alert-profile", default=os.getenv("BHIKSHA_LATHI_PROFILE", "jarvis-northstar"))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    os.chdir(repo_root)

    if _should_skip_for_calendar(args.job, force=args.force):
        _print_result({"job": args.job, "status": "skipped", "reason": "non_trading_day"})
        return 0

    try:
        if args.job == "live-start":
            return _server_session_job(
                args,
                ["restart", "--live", "--post-start-check-seconds", _post_start_check_seconds()],
                repo_root=repo_root,
            )
        if args.job == "live-watchdog":
            return _server_session_job(
                args,
                ["ensure-running", "--live", "--post-start-check-seconds", _post_start_check_seconds()],
                repo_root=repo_root,
            )
        if args.job == "live-stop":
            return _stop_job(args, repo_root=repo_root)
        if args.job == "schwab-refresh":
            return _schwab_refresh_job(args)
        if args.job == "session-report":
            return _session_report_job(args)
    except Exception as exc:  # noqa: BLE001 - scheduled jobs must alert and fail closed.
        alert = _send_failure_alert(args, title=f"Bhiksha launchd job failed: {args.job}", detail=str(exc))
        _print_result({"job": args.job, "status": "failed", "error": str(exc), "alert": alert.to_dict()})
        return 2
    raise ValueError(f"Unsupported job: {args.job}")


def _server_session_job(args: argparse.Namespace, command_args: list[str], *, repo_root: Path) -> int:
    completed = _run_python_module(["bhiksha.tools.server_session", *command_args], repo_root=repo_root)
    if completed.returncode == 0:
        _print_result(
            {
                "job": args.job,
                "status": "ok",
                "return_code": completed.returncode,
                "stdout_tail": _tail(completed.stdout),
                "stderr_tail": _tail(completed.stderr),
            }
        )
        return 0
    alert = _send_failure_alert(
        args,
        title=f"Bhiksha launchd job failed: {args.job}",
        detail=_command_failure_detail(completed),
    )
    _print_result(
        {
            "job": args.job,
            "status": "failed",
            "return_code": completed.returncode,
            "alert": alert.to_dict(),
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
    )
    return 2


def _stop_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    status = _run_python_module(["bhiksha.tools.server_session", "status"], repo_root=repo_root)
    if status.returncode == 0:
        runtime_status = _parse_runtime_status(status.stdout)
        if runtime_status is not None and not runtime_status.get("running"):
            _print_result({"job": args.job, "status": "ok", "detail": "not_running"})
            return 0
    return _server_session_job(args, ["stop"], repo_root=repo_root)


def _schwab_refresh_job(args: argparse.Namespace) -> int:
    browser_cmd = os.getenv(
        "BHIKSHA_SCHWAB_BROWSER_RENEWAL_CMD",
        "/Users/sunny/code/browser-agent/scripts/schwab-auto-refresh.sh",
    )
    command = ["/bin/bash", "-lc", browser_cmd] if browser_cmd else None
    result = run_schwab_token_guard_sync(
        mode="premarket",
        browser_renewal_mode="auto",
        browser_renewal_cmd=command,
        receipt_dir=Path("artifacts/playbook/schwab_token_guard"),
        alert_mode=args.alert_mode,
        alert_profile=args.alert_profile,
    )
    _print_result({"job": args.job, "status": "ok" if result.ok else "failed", "result": result.to_dict()})
    return 0 if result.ok else 2


def _session_report_job(args: argparse.Namespace) -> int:
    report_label = _report_label(args.report_label)
    runtime = build_runtime(active_plan_path=args.active_plan)
    db_path = Path(runtime.app_config.sqlite_path)
    output_dir = Path(runtime.app_config.playbook_artifacts_dir) / "reports"
    result = write_daily_report(db_path, output_dir=output_dir)
    level = _alert_level_for_report(result.report)
    body = render_daily_report_telegram_summary(result.report, markdown_path=result.markdown_path)
    alert = send_lathi_alert(
        title=f"Bhiksha {report_label} session report",
        body=body,
        level=level,
        mode=args.alert_mode,
        profile=args.alert_profile,
    )
    ok = alert.ok or args.alert_mode == "off"
    _print_result(
        {
            "job": args.job,
            "status": "ok" if ok else "failed",
            "report_label": report_label,
            "report_json": str(result.json_path),
            "report_markdown": str(result.markdown_path),
            "report_status": result.report.get("status"),
            "alert": alert.to_dict(),
        }
    )
    return 0 if ok else 2


def _should_skip_for_calendar(job: str, *, force: bool) -> bool:
    if force:
        return False
    if job == "live-stop":
        return False
    today = datetime.now(CENTRAL).date()
    return not is_trading_day(today)


def _run_python_module(args: list[str], *, repo_root: Path) -> subprocess.CompletedProcess[str]:
    module, *rest = args
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", module, *rest],
        check=False,
        cwd=str(repo_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(os.getenv("BHIKSHA_LAUNCHD_JOB_TIMEOUT_SECONDS", "600")),
    )


def _send_failure_alert(args: argparse.Namespace, *, title: str, detail: str):
    body = "\n".join(
        [
            f"Job: {args.job}",
            f"Host repo: {Path.cwd()}",
            "",
            detail,
        ]
    )
    return send_lathi_alert(
        title=title,
        body=body,
        level="error",
        mode=args.alert_mode,
        profile=args.alert_profile,
    )


def _command_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        [
            f"Return code: {completed.returncode}",
            "",
            "stdout:",
            _tail(completed.stdout),
            "",
            "stderr:",
            _tail(completed.stderr),
        ]
    )


def _post_start_check_seconds() -> str:
    return os.getenv("BHIKSHA_POST_START_CHECK_SECONDS", "20")


def _alert_level_for_report(report: dict) -> str:
    level = str((report.get("status") or {}).get("level") or "GREEN").upper()
    if level == "RED":
        return "error"
    if level in {"YELLOW", "NO_DATA"}:
        return "warning"
    return "info"


def _report_label(raw: str) -> str:
    if raw != "scheduled":
        return raw
    now = datetime.now(CENTRAL).time()
    if now.hour < 10:
        return "morning"
    if now.hour < 14:
        return "midday"
    return "close"


def _parse_runtime_status(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith("RUNTIME_STATUS="):
            try:
                value = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _tail(text: str, *, max_lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


def _print_result(payload: dict) -> None:
    print("BHIKSHA_LAUNCHD_JOB=" + json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
