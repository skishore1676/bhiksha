"""Bhiksha-owned launchd job runner."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.app.bootstrap import build_runtime
from bhiksha.config.environment import load_dotenv
from bhiksha.market_data.trading_calendar import is_trading_day
from loguru import logger

from bhiksha.ops.alerts import (
    AlertMode,
    ReviewPublishResult,
    publish_lathi_review,
    send_lathi_alert,
)
from bhiksha.ops.daily_report import (
    DailyReportWriteResult,
    render_daily_report_telegram_summary,
    write_daily_report,
)
from bhiksha.ops.launchd_status_store import write_latest_status
from bhiksha.ops.reconciliation_supervision import run_reconciliation_supervisor
from bhiksha.ops.schwab_token_guard import run_schwab_token_guard_sync
from bhiksha.ops.weekly_trading_decisions import (
    finalize_weekly_trading_decisions,
    write_weekly_trading_decisions,
)

CENTRAL = ZoneInfo("America/Chicago")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job",
        choices=[
            "live-start",
            "live-watchdog",
            "reconciliation-supervisor",
            "live-stop",
            "schwab-refresh",
            "session-report",
            "weekly-trading-decisions",
        ],
    )
    parser.add_argument("--force", action="store_true", help="Run even when today is not a trading day")
    parser.add_argument(
        "--browser-renewal-mode",
        default="auto",
        choices=["off", "auto", "force"],
        help="Schwab browser-renewal policy; force is reserved for a confirmed operator action.",
    )
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--active-plan", default="artifacts/playbook/active_plan.json")
    parser.add_argument("--report-label", default="scheduled")
    parser.add_argument(
        "--week-end",
        help="explicit reporting cutoff for a weekly-decisions replay (YYYY-MM-DD)",
    )
    parser.add_argument("--alert-mode", default=os.getenv("BHIKSHA_LAUNCHD_ALERT_MODE", "live"), choices=["off", "spool", "live"])
    parser.add_argument("--alert-profile", default=os.getenv("BHIKSHA_LATHI_PROFILE", "bhiksha-northstar"))
    parser.add_argument(
        "--obsidian-review-mode",
        default=os.getenv("BHIKSHA_SESSION_REPORT_OBSIDIAN_MODE", "off"),
        choices=["off", "on"],
        help=(
            "Also project the session report onto the passive Bhiksha shelf "
            "via Lathi Bus. Graceful no-op when "
            "the bus is unreachable; never fails the report job."
        ),
    )
    parser.add_argument(
        "--obsidian-review-profile",
        default=os.getenv("BHIKSHA_OBSIDIAN_REVIEW_PROFILE", "bhiksha-northstar"),
    )
    parser.add_argument(
        "--weekly-review-mode",
        default=os.getenv("BHIKSHA_WEEKLY_REVIEW_MODE", "off"),
        choices=["off", "on"],
    )
    parser.add_argument(
        "--workbook-update-mode",
        default=os.getenv("BHIKSHA_WORKBOOK_UPDATE_MODE", "on"),
        choices=["off", "on"],
    )
    parser.add_argument("--action-id", default=os.getenv("BHIKSHA_ACTION_ID"))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    os.chdir(repo_root)
    if args.action_id:
        os.environ["BHIKSHA_ACTION_ID"] = args.action_id

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
                [
                    "ensure-running",
                    "--sync-before-start",
                    "--live",
                    "--post-start-check-seconds",
                    _post_start_check_seconds(),
                ],
                repo_root=repo_root,
            )
        if args.job == "reconciliation-supervisor":
            return _reconciliation_supervisor_job(args, repo_root=repo_root)
        if args.job == "live-stop":
            return _stop_job(args, repo_root=repo_root)
        if args.job == "schwab-refresh":
            return _schwab_refresh_job(args)
        if args.job == "session-report":
            return _session_report_job(args)
        if args.job == "weekly-trading-decisions":
            return _weekly_trading_decisions_job(args, repo_root=repo_root)
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
    local_now = datetime.now(CENTRAL)
    mode = "after_close" if local_now.hour >= 15 else "premarket"
    if args.browser_renewal_mode == "force":
        mode = "operator_reauth"
    result = run_schwab_token_guard_sync(
        mode=mode,
        browser_renewal_mode=args.browser_renewal_mode,
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
    result = write_daily_report(db_path, output_dir=output_dir, deployments=runtime.deployments)
    level = _alert_level_for_report(result.report)
    body = render_daily_report_telegram_summary(result.report, markdown_path=result.markdown_path)
    alert = send_lathi_alert(
        title=f"Bhiksha {report_label} session report",
        body=body,
        level=level,
        mode=args.alert_mode,
        profile=args.alert_profile,
        template="status",
        link_preview="disabled",
    )
    review = _publish_session_report_review(args, result, report_label)
    ok = alert.ok or args.alert_mode == "off"
    payload: dict = {
        "job": args.job,
        "status": "ok" if ok else "failed",
        "report_label": report_label,
        "report_json": str(result.json_path),
        "report_markdown": str(result.markdown_path),
        "report_status": result.report.get("status"),
        "alert": alert.to_dict(),
    }
    if review is not None:
        payload["obsidian_review"] = review.to_dict()
    _print_result(payload)
    return 0 if ok else 2


def _reconciliation_supervisor_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    runtime = build_runtime(active_plan_path=args.active_plan)
    receipt = run_reconciliation_supervisor(
        runtime.app_config.sqlite_path,
        receipt_dir=Path(runtime.app_config.playbook_artifacts_dir) / "reconciliation_supervision",
        alert_mode=args.alert_mode,
        alert_profile=args.alert_profile,
    )
    payload = {
        "job": args.job,
        "status": receipt["job_status"],
        "reconciliation_supervision": receipt,
        "alert": receipt.get("alert"),
        "receipt": str(
            Path(runtime.app_config.playbook_artifacts_dir)
            / "reconciliation_supervision"
            / "latest.json"
        ),
    }
    _print_result(payload)
    return 2 if receipt["attention_required"] else 0


def _weekly_trading_decisions_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    """Refresh the ledger, then publish exactly one Obsidian decision packet."""
    runtime = build_runtime(active_plan_path=args.active_plan)
    output_dir = _weekly_report_output_dir(
        runtime.app_config.playbook_artifacts_dir,
        workbook_update_mode=args.workbook_update_mode,
    )
    result = write_weekly_trading_decisions(
        Path(runtime.app_config.sqlite_path),
        output_dir=output_dir,
        week_end=args.week_end,
        deployments=runtime.deployments,
        exit_edge_db_path=runtime.app_config.exit_edge_live_shadow_db_path,
        exit_edge_status_path=runtime.app_config.exit_edge_live_shadow_status_path,
        exit_edge_collector_configured=(
            runtime.app_config.exit_edge_live_shadow_enabled
        ),
    )
    workbook = _update_trading_decision_ledger(args, result.facts_path, repo_root=repo_root)
    result = finalize_weekly_trading_decisions(result, workbook)
    if workbook.get("status") == "skipped" and args.weekly_review_mode == "off":
        _print_result({
            "job": args.job,
            "status": "ok",
            "preview_only": True,
            "report_json": str(result.json_path),
            "report_markdown": str(result.markdown_path),
            "facts_export": str(result.facts_path),
            "governance_evidence": str(result.governance_path),
            "exit_edge_evidence": str(result.exit_edge_path),
            "telegram_sent": False,
        })
        return 0
    if workbook.get("status") != "ok":
        _print_result({
            "job": args.job,
            "status": "failed",
            "reason": "workbook_update_failed",
            "report_json": str(result.json_path),
            "report_markdown": str(result.markdown_path),
            "workbook_update": workbook,
        })
        return 2
    review: ReviewPublishResult | None = None
    if args.weekly_review_mode == "on":
        review = publish_lathi_review(
            source=result.markdown_path,
            title=f"Weekly Trading Decisions — Performance, Promotions & Fixes — {result.report['week_end']}",
            mode="on",
            profile=args.obsidian_review_profile,
            workspace_root=Path.cwd(),
            artifact_id=result.report["artifact_id"],
            owner_consumer="bhiksha",
            resume_mode="automatic",
            review_id=result.report["artifact_id"],
        )
    ok = args.weekly_review_mode == "off" or bool(review and review.ok)
    _print_result({
        "job": args.job,
        "status": "ok" if ok else "failed",
        "artifact_id": result.report["artifact_id"],
        "report_json": str(result.json_path),
        "report_markdown": str(result.markdown_path),
        "facts_export": str(result.facts_path),
        "governance_evidence": str(result.governance_path),
        "exit_edge_evidence": str(result.exit_edge_path),
        "workbook_update": workbook,
        "obsidian_review": review.to_dict() if review else None,
        "telegram_sent": False,
    })
    return 0 if ok else 2


def _weekly_report_output_dir(
    playbook_artifacts_dir: str | Path,
    *,
    workbook_update_mode: str,
) -> Path:
    """Keep preview evidence from replacing the last passing weekly receipt."""
    reports = Path(playbook_artifacts_dir) / "reports"
    if workbook_update_mode == "off":
        return reports / "previews"
    return reports


def _update_trading_decision_ledger(
    args: argparse.Namespace,
    facts_path: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    if args.workbook_update_mode == "off":
        return {"status": "skipped", "reason": "workbook update intentionally disabled"}
    command_text = os.getenv(
        "BHIKSHA_WORKBOOK_UPDATE_COMMAND",
        "/Users/sunny/code/tradelab/scripts/review/update_trading_decision_ledger.sh",
    )
    command = [command_text, str(facts_path)]
    completed = subprocess.run(
        command,
        check=False,
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(os.getenv("BHIKSHA_WORKBOOK_UPDATE_TIMEOUT_SECONDS", "180")),
    )
    receipt = _last_json_object(completed.stdout)
    if completed.returncode != 0 or receipt.get("status") != "ok":
        return {
            "status": "failed",
            "return_code": completed.returncode,
            "error": receipt.get("error") or _tail(completed.stderr or completed.stdout),
        }
    return receipt


def _last_json_object(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _publish_session_report_review(
    args: argparse.Namespace,
    result: DailyReportWriteResult,
    report_label: str,
) -> ReviewPublishResult | None:
    """Project the session report onto the Obsidian approve/archive surface.

    Additive projection only: the Telegram alert remains the primary operator
    channel and owns the report job's success. This publish is transport-graceful
    and is deliberately isolated from the return code -- an unreachable review
    bus logs a warning and is surfaced in the ``obsidian_review`` payload, but
    never turns a healthy trading-report run into a failed launchd job.
    """
    mode = args.obsidian_review_mode
    if mode == "off":
        return None
    trading_date = result.report.get("trading_date")
    title = f"Bhiksha {report_label} session report - {trading_date}"
    try:
        review = publish_lathi_review(
            source=result.markdown_path,
            title=title,
            mode=mode,
            profile=args.obsidian_review_profile,
            workspace_root=Path.cwd(),
            artifact_id=_relative_artifact_id(result.markdown_path),
            owner_consumer="bhiksha",
            passive=True,
        )
    except Exception as exc:  # noqa: BLE001 - projection must never fail the job.
        logger.warning("Obsidian session-report review projection crashed: {}", exc)
        return ReviewPublishResult(attempted=True, ok=False, mode=mode, error=str(exc))
    if not review.ok:
        logger.warning(
            "Obsidian session-report review not published (mode={}): {}",
            mode,
            review.error or "no note_path in receipt",
        )
    return review


def _relative_artifact_id(markdown_path: Path) -> str:
    try:
        return str(markdown_path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(markdown_path)


def _should_skip_for_calendar(job: str, *, force: bool) -> bool:
    if force:
        return False
    # live-stop must always run so a stale process cannot survive; the weekly
    # scorecard is the week's verdict and must publish even when the Friday it
    # fires is itself a market holiday (the Mon-Fri window still had trading).
    if job in {"live-stop", "weekly-trading-decisions"}:
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
    status = report.get("status") or {}
    level = str(status.get("level") or "GREEN").upper()
    if status.get("attention_required") is False:
        return "info"
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
    payload = dict(payload)
    action_id = os.getenv("BHIKSHA_ACTION_ID")
    if action_id and "action_id" not in payload:
        payload["action_id"] = action_id
    _write_latest_status(payload)
    print("BHIKSHA_LAUNCHD_JOB=" + json.dumps(payload, sort_keys=True, default=str))


def _write_latest_status(payload: dict) -> None:
    try:
        write_latest_status(Path.cwd(), payload)
    except Exception:
        # Status snapshots are observational. They must never turn a successful
        # trading-domain job into a failed launchd job.
        return


if __name__ == "__main__":
    raise SystemExit(main())
