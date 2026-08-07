"""Bhiksha-owned launchd job runner."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from bhiksha.app.bootstrap import build_runtime
from bhiksha.config.environment import load_dotenv
from bhiksha.market_data.trading_calendar import is_trading_day
from bhiksha.ops.alerts import (
    ReviewPublishResult,
    publish_lathi_review,
    send_lathi_alert,
)
from bhiksha.ops.daily_report import (
    DailyReportWriteResult,
    render_daily_report_ryg_telegram_html,
    render_daily_report_ryg_telegram_text,
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
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    chart_job = "chart-scenario-shadow" in raw_argv
    if not chart_job:
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
            "chart-scenario-shadow",
        ],
    )
    parser.add_argument(
        "--force", action="store_true", help="Run even when today is not a trading day"
    )
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
    parser.add_argument(
        "--alert-mode",
        default=os.getenv("BHIKSHA_LAUNCHD_ALERT_MODE", "live"),
        choices=["off", "spool", "live"],
    )
    parser.add_argument(
        "--alert-profile",
        default=os.getenv("BHIKSHA_LATHI_PROFILE", "bhiksha-northstar"),
    )
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
    args = parser.parse_args(raw_argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    os.chdir(repo_root)
    if args.action_id:
        os.environ["BHIKSHA_ACTION_ID"] = args.action_id

    if _should_skip_for_calendar(args.job, force=args.force):
        _print_result(
            {"job": args.job, "status": "skipped", "reason": "non_trading_day"}
        )
        return 0

    try:
        if args.job == "live-start":
            return _server_session_job(
                args,
                [
                    "restart",
                    "--live",
                    "--post-start-check-seconds",
                    _post_start_check_seconds(),
                ],
                repo_root=repo_root,
            )
        if args.job == "live-watchdog":
            # Freshness check: a running process on a stale active_plan is
            # not healthy — it will keep trading yesterday's deployments.
            # Instead of bubbling to the operator, try a fresh restart here.
            if _should_watchdog_refresh(repo_root=repo_root, args=args):
                return _server_session_job(
                    args,
                    [
                        "restart",
                        "--live",
                        "--post-start-check-seconds",
                        _post_start_check_seconds(),
                    ],
                    repo_root=repo_root,
                )
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
        if args.job == "chart-scenario-shadow":
            return _chart_scenario_shadow_job(args, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 - scheduled jobs must alert and fail closed.
        alert = _send_failure_alert(
            args, title=f"Bhiksha launchd job failed: {args.job}", detail=str(exc)
        )
        _print_result(
            {
                "job": args.job,
                "status": "failed",
                "error": str(exc),
                "alert": alert.to_dict(),
            }
        )
        return 2
    raise ValueError(f"Unsupported job: {args.job}")


def _server_session_job(
    args: argparse.Namespace, command_args: list[str], *, repo_root: Path
) -> int:
    completed = _run_python_module(
        ["bhiksha.tools.server_session", *command_args], repo_root=repo_root
    )
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
    status = _run_python_module(
        ["bhiksha.tools.server_session", "status"], repo_root=repo_root
    )
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
    _print_result(
        {
            "job": args.job,
            "status": "ok" if result.ok else "failed",
            "result": result.to_dict(),
        }
    )
    return 0 if result.ok else 2


def _session_report_job(args: argparse.Namespace) -> int:
    report_label = _report_label(args.report_label)
    runtime = build_runtime(active_plan_path=args.active_plan)
    db_path = Path(runtime.app_config.sqlite_path)
    output_dir = Path(runtime.app_config.playbook_artifacts_dir) / "reports"
    # Gather live probes for RYG APP block (best-effort, don't fail report)
    app_status: dict[str, Any] | None = None
    schwab_status: dict[str, Any] | None = None
    try:
        from bhiksha.tools.server_session import _runtime_status as _get_runtime_status
        import json as _json

        raw = _get_runtime_status(runtime.app_config.sqlite_path)  # fallback path
        # _runtime_status expects repo_root; try direct probe via server_session tool
        import subprocess as _sp
        import sys as _sys

        repo_root = Path(__file__).resolve().parents[2]
        py = str(repo_root / ".venv" / "bin" / "python") if (repo_root / ".venv" / "bin" / "python").exists() else _sys.executable
        cp = _sp.run(
            [py, "-m", "bhiksha.tools.server_session", "status"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        for line in cp.stdout.splitlines():
            if line.startswith("RUNTIME_STATUS="):
                app_status = _json.loads(line.split("=", 1)[1])
                break
    except Exception:
        app_status = None
    try:
        schwab_path = Path(runtime.app_config.playbook_artifacts_dir) / "schwab_token_guard" / "latest.json"
        if schwab_path.is_file():
            schwab_status = json.loads(schwab_path.read_text())
    except Exception:
        schwab_status = None
    result = write_daily_report(
        db_path,
        output_dir=output_dir,
        deployments=runtime.deployments,
        app_status=app_status,
        schwab_status=schwab_status,
    )
    level = _alert_level_for_report(result.report)
    # User prefers RYG tables (APP/LIVE/SHADOW) - use HTML <pre> tables via Lathi
    try:
        body = render_daily_report_ryg_telegram_html(
            result.report, app_status=app_status, schwab_status=schwab_status
        )
    except Exception:
        body = render_daily_report_ryg_telegram_text(
            result.report, app_status=app_status, schwab_status=schwab_status
        )
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
        receipt_dir=Path(runtime.app_config.playbook_artifacts_dir)
        / "reconciliation_supervision",
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
    workbook = _update_trading_decision_ledger(
        args, result.facts_path, repo_root=repo_root
    )
    result = finalize_weekly_trading_decisions(result, workbook)
    if workbook.get("status") == "skipped" and args.weekly_review_mode == "off":
        _print_result(
            {
                "job": args.job,
                "status": "ok",
                "preview_only": True,
                "report_json": str(result.json_path),
                "report_markdown": str(result.markdown_path),
                "facts_export": str(result.facts_path),
                "governance_evidence": str(result.governance_path),
                "exit_edge_evidence": str(result.exit_edge_path),
                "telegram_sent": False,
            }
        )
        return 0
    if workbook.get("status") != "ok":
        _print_result(
            {
                "job": args.job,
                "status": "failed",
                "reason": "workbook_update_failed",
                "report_json": str(result.json_path),
                "report_markdown": str(result.markdown_path),
                "workbook_update": workbook,
            }
        )
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
    _print_result(
        {
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
        }
    )
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


def _chart_scenario_shadow_job(args: argparse.Namespace, *, repo_root: Path) -> int:
    completed = _run_python_module(
        ["bhiksha.tools.chart_scenario_coordinator", "--phase", "auto"],
        repo_root=repo_root,
        env=_chart_subprocess_env(repo_root),
    )
    payload = _last_json_object(completed.stdout)
    status = str(payload.get("status") or "failed")
    ok = completed.returncode == 0 and status in {"succeeded", "skipped"}
    _print_result(
        {
            "job": args.job,
            "status": "ok" if ok else "failed",
            "coordinator_status": status,
            "phase": payload.get("phase"),
            "receipt_hash": payload.get("content_hash"),
            "reason": payload.get("reason"),
            "return_code": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        }
    )
    return 0 if ok else 2


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
        capture_output=True,
        timeout=float(os.getenv("BHIKSHA_WORKBOOK_UPDATE_TIMEOUT_SECONDS", "180")),
    )
    receipt = _last_json_object(completed.stdout)
    if completed.returncode != 0 or receipt.get("status") != "ok":
        return {
            "status": "failed",
            "return_code": completed.returncode,
            "error": receipt.get("error")
            or _tail(completed.stderr or completed.stdout),
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


def _run_python_module(
    args: list[str], *, repo_root: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    module, *rest = args
    child_env = os.environ.copy() if env is None else dict(env)
    kernel_src = child_env.get("BHIKSHA_KERNEL_SRC", "").strip()
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(repo_root / "src"), kernel_src) if item
    )
    child_env["PYTHONUNBUFFERED"] = "1"
    if env is not None:
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["BHIKSHA_SANITIZED_SUBPROCESS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", module, *rest],
        check=False,
        cwd=str(repo_root),
        env=child_env,
        text=True,
        capture_output=True,
        timeout=float(os.getenv("BHIKSHA_LAUNCHD_JOB_TIMEOUT_SECONDS", "600")),
    )


def _chart_subprocess_env(repo_root: Path) -> dict[str, str]:
    """Return the exact first-hop environment for the chart coordinator."""

    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "TMPDIR",
        "BHIKSHA_KERNEL_SRC",
        "BHIKSHA_CHART_KERNEL_RUNTIME_RECORD",
        "BHIKSHA_CHART_KERNEL_RUNTIME_HASH",
        "BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED",
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT",
        "BHIKSHA_CHART_SCENARIO_CAMPAIGN_CONFIG",
        "BHIKSHA_CHART_SCENARIO_DAILY_CONTRACT_DIR",
        "BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH",
        "SCHWAB_TOKEN_FILE",
        "SCHWAB_API_BASE_URL",
        "SCHWAB_TIMEOUT_SECONDS",
        "BHIKSHA_CHART_SCENARIO_COMMAND_TIMEOUT_SECONDS",
        "BHIKSHA_LAUNCHD_JOB_TIMEOUT_SECONDS",
    }
    child = {key: value for key, value in os.environ.items() if key in allowed}
    child["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    child["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (
            str(repo_root / "src"),
            child.get("BHIKSHA_KERNEL_SRC", "").strip(),
        )
        if item
    )
    child["PYTHONUNBUFFERED"] = "1"
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    child["BHIKSHA_SANITIZED_SUBPROCESS"] = "1"
    return child


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


def _should_watchdog_refresh(*, repo_root: Path, args: argparse.Namespace) -> bool:
    """Return True when the live runtime is running but on a stale plan.

    The watchdog previously only healed a dead process. After the Aug 6
    dep incident the process stayed alive on yesterday's plan (2026-08-05)
    while today's live-start failed to write a fresh plan. That stale
    session still traded at 09:39. Instead of leaving it to the operator,
    the next watchdog should attempt a fresh restart; if the restart fails
    (e.g. canary pin mismatch) it will be surfaced as a failed job.
    """
    # Need a running process to be worth refreshing
    try:
        from bhiksha.tools.server_session import _runtime_status
        from bhiksha.ops.launchd_registry import latest_status_path

        pid_path = Path(args.pid_path) if hasattr(args, "pid_path") else repo_root / "artifacts" / "playbook" / "runtime" / "bhiksha.pid"
        status = _runtime_status(pid_path)
        if not status.get("running"):
            return False
    except Exception:  # noqa: BLE001
        return False

    # Check active_plan freshness
    plan_path = Path(args.active_plan) if hasattr(args, "active_plan") else repo_root / "artifacts" / "playbook" / "active_plan.json"
    if not plan_path.is_absolute():
        plan_path = repo_root / plan_path
    try:
        if not plan_path.is_file():
            return True
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        trading_date = str(payload.get("trading_date") or "")
        generated_at_raw = str(payload.get("generated_at") or "")
        today = datetime.now(CENTRAL).date().isoformat()
        # If today is a trading day and plan's trading_date is not today, stale
        if is_trading_day(datetime.now(CENTRAL).date()):
            if trading_date and trading_date != today:
                return True
            # Also consider a plan generated >26h ago as stale across a session boundary
            if generated_at_raw:
                try:
                    gen = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
                    if gen.tzinfo is None:
                        gen = gen.replace(tzinfo=ZoneInfo("UTC"))
                    age_hours = (datetime.now(ZoneInfo("UTC")) - gen.astimezone(ZoneInfo("UTC"))).total_seconds() / 3600.0
                    if age_hours > 26:
                        return True
                except Exception:  # noqa: BLE001
                    pass
        return False
    except Exception:  # noqa: BLE001
        return False


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
        if payload.get("job") == "chart-scenario-shadow":
            # Imported lazily — only chart jobs need the kernel.
            from bhiksha.chart_scenarios.paths import require_experiment_path  # noqa: WPS433
            chart_root = Path(
                os.getenv(
                    "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT",
                    str(Path.cwd() / "artifacts" / "chart_scenarios"),
                )
            )
            if not chart_root.is_absolute():
                raise ValueError("chart launchd artifact root must be absolute")
            requested = chart_root / "launchd" / "latest_status.json"
            path = require_experiment_path(requested, role="launchd status receipt")
            if any(
                parent.is_symlink() for parent in requested.parents if parent.exists()
            ):
                raise ValueError("chart launchd status parent cannot be a symlink")
            if requested.is_symlink():
                raise ValueError("chart launchd status target cannot be a symlink")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "schema": "bhiksha.chart-scenario-launchd-status.v1",
                                "recorded_at": datetime.now(CENTRAL).isoformat(),
                                "payload": payload,
                            },
                            indent=2,
                            sort_keys=True,
                            default=str,
                        )
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
            return
        write_latest_status(Path.cwd(), payload)
    except (OSError, TypeError, ValueError):
        # Status snapshots are observational. They must never turn a successful
        # trading-domain job into a failed launchd job.
        return


if __name__ == "__main__":
    raise SystemExit(main())
