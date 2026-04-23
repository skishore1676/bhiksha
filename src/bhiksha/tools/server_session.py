"""Server-side helper to sync the active sheet and manage the Bhiksha runtime process."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from bhiksha.config.environment import load_dotenv
from bhiksha.tools.sync_active_plan import sync_active_plan_once


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    defaults = _defaults()

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Sync the Google Sheet into the active plan JSON")
    _add_sync_args(sync, defaults)

    start = subparsers.add_parser("start", help="Start Bhiksha as a background process")
    _add_runtime_args(start, defaults)

    stop = subparsers.add_parser("stop", help="Stop the background Bhiksha process")
    _add_common_process_args(stop, defaults)
    stop.add_argument("--timeout-seconds", type=float, default=15.0, help="How long to wait before escalating to SIGKILL")

    status = subparsers.add_parser("status", help="Show background Bhiksha process status")
    _add_common_process_args(status, defaults)

    ensure_running = subparsers.add_parser("ensure-running", help="Start Bhiksha when it is not already running")
    _add_runtime_args(ensure_running, defaults)

    restart = subparsers.add_parser("restart", help="Sync the active plan and restart Bhiksha in one step")
    _add_sync_args(restart, defaults)
    _add_runtime_args(restart, defaults, include_active_plan=False)
    restart.add_argument("--timeout-seconds", type=float, default=15.0, help="How long to wait before escalating to SIGKILL")

    args = parser.parse_args(argv)
    if args.command == "sync":
        result = _sync_from_args(args)
        _print_sync_result(result)
        return 0
    if args.command == "start":
        info = _start_runtime(args)
        _print_status(info)
        return 0
    if args.command == "stop":
        info = _stop_runtime(Path(args.pid_path), timeout_seconds=args.timeout_seconds)
        _print_status(info)
        return 0
    if args.command == "status":
        _print_status(_runtime_status(Path(args.pid_path)))
        return 0
    if args.command == "ensure-running":
        info = _runtime_status(Path(args.pid_path))
        if info["running"]:
            _print_status({"action": "ensure-running", **info})
            return 0
        start_info = _start_runtime(args)
        _print_status({"action": "ensure-running", **start_info})
        return 0
    if args.command == "restart":
        result = _sync_from_args(args)
        stop_info = _stop_runtime(Path(args.pid_path), timeout_seconds=args.timeout_seconds, missing_ok=True)
        start_info = _start_runtime(args)
        _print_sync_result(result)
        _print_status(stop_info)
        _print_status(start_info)
        return 0
    raise ValueError(f"Unsupported command: {args.command}")


def _defaults() -> dict[str, str | None]:
    repo_root = Path(__file__).resolve().parents[3]
    return {
        "google_sheet_id": os.getenv("GOOGLE_SHEET_ID"),
        "credentials_path": os.getenv("GOOGLE_API_CREDENTIALS_PATH"),
        "catalog_sheet_name": os.getenv("STRATEGY_CATALOG_SHEET_NAME", "strategy catalog"),
        "strategy_sheet_name": os.getenv("ACTIVE_STRATEGIES_SHEET_NAME", "active_strategies"),
        "manual_sheet_name": os.getenv("MANUAL_ENTRY_SHEET_NAME") or os.getenv("MANNUAL_ENTRY_SHEET_NAME") or "manual_entry",
        "strategy_catalog_path": os.getenv("BHIKSHA_STRATEGY_CATALOG_PATH", "config/strategy_catalog"),
        "active_plan_path": os.getenv("BHIKSHA_ACTIVE_PLAN_PATH", "artifacts/playbook/active_plan.json"),
        "log_dir": os.getenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", "artifacts/playbook/logs"),
        "source_name": os.getenv("BHIKSHA_ACTIVE_PLAN_SOURCE_NAME", "google_sheet_integration"),
        "pid_path": os.getenv("BHIKSHA_RUNTIME_PID_PATH", "artifacts/playbook/runtime/bhiksha.pid"),
        "runtime_log_dir": os.getenv("BHIKSHA_RUNTIME_LOG_DIR", "artifacts/playbook/runtime"),
        "python_executable": os.getenv("BHIKSHA_RUNTIME_PYTHON", sys.executable),
        "repo_root": str(repo_root),
    }


def _add_sync_args(parser: argparse.ArgumentParser, defaults: dict[str, str | None]) -> None:
    parser.add_argument("--google-sheet-id", default=defaults["google_sheet_id"], help="Google spreadsheet URL or ID")
    parser.add_argument("--credentials-path", default=defaults["credentials_path"], help="Google service-account credentials JSON path")
    parser.add_argument("--catalog-sheet-name", default=defaults["catalog_sheet_name"], help="Strategy catalog sheet name")
    parser.add_argument("--strategy-sheet-name", default=defaults["strategy_sheet_name"], help="Active strategy sheet name")
    parser.add_argument("--manual-sheet-name", default=defaults["manual_sheet_name"], help="Manual entry sheet name")
    parser.add_argument("--strategy-catalog", default=defaults["strategy_catalog_path"], help="Local Bhiksha strategy catalog path")
    parser.add_argument("--active-plan", default=defaults["active_plan_path"], help="Where to write the compiled active plan JSON")
    parser.add_argument("--sync-log-dir", default=defaults["log_dir"], help="Directory for active-plan sync logs")
    parser.add_argument("--active-plan-id", default=None, help="Optional explicit active_plan_id")
    parser.add_argument("--trading-date", default=None, help="Optional trading date in YYYY-MM-DD format")
    parser.add_argument("--source-name", default=defaults["source_name"], help="Recorded source.name for this plan")


def _add_common_process_args(parser: argparse.ArgumentParser, defaults: dict[str, str | None]) -> None:
    parser.add_argument("--pid-path", default=defaults["pid_path"], help="PID metadata file for the background runtime")


def _add_runtime_args(
    parser: argparse.ArgumentParser,
    defaults: dict[str, str | None],
    *,
    include_active_plan: bool = True,
) -> None:
    _add_common_process_args(parser, defaults)
    parser.add_argument("--runtime-log-dir", default=defaults["runtime_log_dir"], help="Directory for background runtime logs")
    if include_active_plan:
        parser.add_argument("--active-plan", default=defaults["active_plan_path"], help="Path to the active plan JSON")
    parser.add_argument("--python-executable", default=defaults["python_executable"], help="Python executable used to launch Bhiksha")
    parser.add_argument("--repo-root", default=defaults["repo_root"], help="Repository root used as the subprocess working directory")
    parser.add_argument("--live", action="store_true", help="Run Bhiksha in live mode")
    parser.add_argument("--max-bars", type=int, default=None, help="Optional max-bars limit for the runtime")


def _sync_from_args(args: argparse.Namespace):
    if not args.google_sheet_id:
        raise ValueError("--google-sheet-id or GOOGLE_SHEET_ID is required")
    if not args.credentials_path:
        raise ValueError("--credentials-path or GOOGLE_API_CREDENTIALS_PATH is required")
    return sync_active_plan_once(
        spreadsheet_id=args.google_sheet_id,
        credentials_path=args.credentials_path,
        catalog_sheet_name=args.catalog_sheet_name,
        strategy_sheet_name=args.strategy_sheet_name,
        manual_sheet_name=args.manual_sheet_name,
        strategy_catalog_path=args.strategy_catalog,
        output_path=args.active_plan,
        log_dir=args.sync_log_dir,
        active_plan_id=args.active_plan_id,
        trading_date=args.trading_date,
        source_name=args.source_name,
    )


def _start_runtime(args: argparse.Namespace) -> dict[str, object]:
    pid_path = Path(args.pid_path).resolve()
    existing = _runtime_status(pid_path)
    if existing["running"]:
        raise RuntimeError(f"Bhiksha is already running with pid={existing['pid']}")

    runtime_log_dir = Path(args.runtime_log_dir).resolve()
    runtime_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_log_dir / f"trade_session_{datetime.now(UTC).date().isoformat()}.log"
    repo_root = Path(args.repo_root).resolve()
    active_plan = Path(args.active_plan).resolve()
    command = [args.python_executable, "-u", "-m", "bhiksha.tools.trade_session", "--active-plan", str(active_plan)]
    if args.live:
        command.append("--live")
    if args.max_bars is not None:
        command.extend(["--max-bars", str(args.max_bars)])

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=str(repo_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    metadata = {
        "pid": process.pid,
        "started_at": datetime.now(UTC).isoformat(),
        "log_path": str(log_path),
        "active_plan_path": str(active_plan),
        "live": bool(args.live),
        "max_bars": args.max_bars,
        "command": command,
        "repo_root": str(repo_root),
    }
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"action": "started", "running": True, **metadata}


def _stop_runtime(pid_path: Path, *, timeout_seconds: float, missing_ok: bool = False) -> dict[str, object]:
    status = _runtime_status(pid_path)
    if not status["running"]:
        if missing_ok:
            return {"action": "stopped", "running": False, "pid": status.get("pid"), "detail": "not_running"}
        raise RuntimeError("Bhiksha is not currently running")

    pid = int(status["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            break
        time.sleep(0.2)
    else:
        os.kill(pid, signal.SIGKILL)
        while _pid_is_running(pid):
            time.sleep(0.1)
    if pid_path.exists():
        pid_path.unlink()
    return {"action": "stopped", "running": False, "pid": pid, "detail": "terminated"}


def _runtime_status(pid_path: Path) -> dict[str, object]:
    resolved = pid_path.resolve()
    if not resolved.exists():
        return {"action": "status", "running": False, "pid": None, "pid_path": str(resolved), "detail": "missing_pid_file"}
    metadata = json.loads(resolved.read_text(encoding="utf-8"))
    pid = int(metadata["pid"])
    running = _pid_is_running(pid)
    if not running:
        return {"action": "status", "running": False, "pid": pid, "pid_path": str(resolved), "detail": "stale_pid_file", **metadata}
    return {"action": "status", "running": True, "pid": pid, "pid_path": str(resolved), "detail": "running", **metadata}


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _print_sync_result(result) -> None:
    print(f"ACTIVE_PLAN={result.active_plan_path}")
    print(f"ACTIVE_PLAN_ID={result.active_plan_id}")
    print("SUMMARY=" + json.dumps(result.summary, sort_keys=True))
    print(f"SUPPRESSED_COUNT={len(result.suppressed)}")
    print(f"ACTIVE_PLAN_UPDATED={'1' if result.changed else '0'}")
    print(f"SYNC_LOG={result.log_path}")


def _print_status(info: dict[str, object]) -> None:
    print("RUNTIME_STATUS=" + json.dumps(info, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
