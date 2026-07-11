"""Run safe Bhiksha launchd actions for Lathi Control Tower."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, time
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo
from typing import Any, Iterator

from bhiksha.config.environment import load_dotenv
from bhiksha.market_data.trading_calendar import is_trading_day
from bhiksha.ops.launchd_registry import control_lock_dir

CENTRAL = ZoneInfo("America/Chicago")

RUNNER_ACTIONS: dict[str, list[str]] = {
    "session-report-now": ["session-report", "--force", "--report-label", "manual"],
    "weekly-trading-decisions-now": ["weekly-trading-decisions", "--force"],
    "schwab-guard-now": ["schwab-refresh", "--force"],
    "ensure-live-runtime": ["live-watchdog", "--force"],
}

READ_ONLY_ACTIONS = {"live-status"}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=sorted((*RUNNER_ACTIONS.keys(), *READ_ONLY_ACTIONS)))
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--action-id", default=None)
    parser.add_argument("--confirm", action="store_true", help="Confirm a trading-runtime action that could start/affect live runtime")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON; accepted for Control Tower compatibility")
    parser.add_argument("--lock-stale-seconds", type=float, default=1800.0)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    action_id = args.action_id or f"bhiksha-{args.action}-{uuid.uuid4().hex[:12]}"
    result = run_control_action(
        action=args.action,
        repo_root=repo_root,
        action_id=action_id,
        confirmed=args.confirm,
        lock_stale_seconds=args.lock_stale_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else int(result.get("exit_code") or 2)


def run_control_action(
    *,
    action: str,
    repo_root: Path,
    action_id: str,
    confirmed: bool = False,
    lock_stale_seconds: float = 1800.0,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    if action == "live-status":
        return _live_status(repo_root=repo_root, action=action, action_id=action_id, started_at=started_at)
    if action not in RUNNER_ACTIONS:
        return _result(action=action, action_id=action_id, started_at=started_at, ok=False, status="refused", reason="unknown_action", exit_code=2)

    confirmation = _confirmation_requirement(action, repo_root=repo_root)
    if confirmation["required"] and not confirmed:
        return _result(
            action=action,
            action_id=action_id,
            started_at=started_at,
            ok=False,
            status="refused",
            reason="confirmation_required",
            exit_code=3,
            confirmation=confirmation,
        )

    try:
        with _action_lock(repo_root=repo_root, action=action, action_id=action_id, stale_seconds=lock_stale_seconds) as lock_info:
            return _run_runner_action(
                repo_root=repo_root,
                action=action,
                action_id=action_id,
                started_at=started_at,
                confirmed=confirmed,
                confirmation=confirmation,
                lock=lock_info,
            )
    except ActionAlreadyRunning as exc:
        return _result(
            action=action,
            action_id=action_id,
            started_at=started_at,
            ok=False,
            status="refused",
            reason="action_already_running",
            exit_code=3,
            in_flight=exc.lock_payload,
        )


def _run_runner_action(
    *,
    repo_root: Path,
    action: str,
    action_id: str,
    started_at: str,
    confirmed: bool,
    confirmation: dict[str, Any],
    lock: dict[str, Any],
) -> dict[str, Any]:
    command = ["scripts/launchd/run_bhiksha_job.sh", *RUNNER_ACTIONS[action], "--action-id", action_id]
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(os.getenv("BHIKSHA_LAUNCHD_CONTROL_TIMEOUT_SECONDS", "900")),
    )
    payload = _parse_prefixed_json(completed.stdout, "BHIKSHA_LAUNCHD_JOB=")
    return _result(
        action=action,
        action_id=action_id,
        started_at=started_at,
        ok=completed.returncode == 0,
        status="succeeded" if completed.returncode == 0 else "failed",
        exit_code=0 if completed.returncode == 0 else 2,
        command=command,
        return_code=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
        bhiksha_job=payload,
        confirmed=confirmed,
        confirmation=confirmation,
        lock=lock,
    )


def _live_status(*, repo_root: Path, action: str, action_id: str, started_at: str) -> dict[str, Any]:
    completed = _run_server_session_status(repo_root)
    payload = _parse_prefixed_json(completed.stdout, "RUNTIME_STATUS=")
    return _result(
        action=action,
        action_id=action_id,
        started_at=started_at,
        ok=completed.returncode == 0,
        status="succeeded" if completed.returncode == 0 else "failed",
        exit_code=0 if completed.returncode == 0 else 2,
        command=[_bhiksha_python(repo_root), "-m", "bhiksha.tools.server_session", "status"],
        return_code=completed.returncode,
        runtime=payload,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
    )


def _confirmation_requirement(action: str, *, repo_root: Path) -> dict[str, Any]:
    if action != "ensure-live-runtime":
        return {"required": False, "reasons": []}
    reasons = []
    now = datetime.now(CENTRAL)
    if is_trading_day(now.date()) and time(8, 30) <= now.time() <= time(15, 0):
        reasons.append("market_open")
    runtime = _runtime_status_payload(repo_root)
    if runtime is not None and not runtime.get("running"):
        reasons.append("would_start_stopped_live_runtime")
    return {"required": bool(reasons), "reasons": reasons, "runtime": runtime}


def _runtime_status_payload(repo_root: Path) -> dict[str, Any] | None:
    completed = _run_server_session_status(repo_root)
    if completed.returncode != 0:
        return None
    parsed = _parse_prefixed_json(completed.stdout, "RUNTIME_STATUS=")
    return parsed if isinstance(parsed, dict) else None


def _run_server_session_status(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [_bhiksha_python(repo_root), "-m", "bhiksha.tools.server_session", "status"],
        check=False,
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
    )


def _bhiksha_python(repo_root: Path) -> str:
    candidate = repo_root / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return sys.executable


class ActionAlreadyRunning(RuntimeError):
    def __init__(self, lock_payload: dict[str, Any]) -> None:
        super().__init__("action already running")
        self.lock_payload = lock_payload


@contextmanager
def _action_lock(*, repo_root: Path, action: str, action_id: str, stale_seconds: float) -> Iterator[dict[str, Any]]:
    directory = control_lock_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{action}.lock"
    payload = {"action": action, "action_id": action_id, "pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = _read_lock(path)
            age = _lock_age_seconds(existing)
            if age is not None and age > stale_seconds:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise ActionAlreadyRunning(existing)
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            break
    try:
        yield {**payload, "path": str(path)}
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"path": str(path), "unreadable": True}
    return parsed if isinstance(parsed, dict) else {"path": str(path), "unreadable": True}


def _lock_age_seconds(payload: dict[str, Any]) -> float | None:
    started = payload.get("started_at")
    if not started:
        return None
    try:
        moment = datetime.fromisoformat(str(started))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds()


def _result(*, action: str, action_id: str, started_at: str, ok: bool, status: str, exit_code: int, **extra: Any) -> dict[str, Any]:
    return {
        "schema": "bhiksha.launchd.control_result.v1",
        "action": action,
        "action_id": action_id,
        "ok": ok,
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": exit_code,
        **extra,
    }


def _parse_prefixed_json(text: str, prefix: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            parsed = json.loads(line.split("=", 1)[1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tail(text: str, *, max_lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


if __name__ == "__main__":
    raise SystemExit(main())
