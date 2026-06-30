"""Emit a Bhiksha launchd/control status snapshot for external observers."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from bhiksha.config.environment import load_dotenv
from bhiksha.ops.launchd_registry import active_launchd_jobs, latest_status_path


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--active-plan", default="artifacts/playbook/active_plan.json")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON; accepted for Control Tower compatibility")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root or Path(__file__).resolve().parents[3]).resolve()
    payload = build_status_snapshot(repo_root=repo_root, active_plan_path=Path(args.active_plan))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def build_status_snapshot(*, repo_root: Path, active_plan_path: Path) -> dict[str, Any]:
    latest = _read_json(latest_status_path(repo_root))
    latest_jobs = latest.get("jobs") if isinstance(latest.get("jobs"), dict) else {}
    launchd = _launchd_state()
    jobs = []
    for spec in active_launchd_jobs():
        latest_record = latest_jobs.get(spec.runner_job) if isinstance(latest_jobs, dict) else None
        latest_payload = (latest_record or {}).get("payload") if isinstance(latest_record, dict) else None
        if not isinstance(latest_payload, dict):
            latest_payload = _latest_log_payload(spec.stdout_log(repo_root))
        jobs.append(
            {
                "label": spec.label,
                "runner_job": spec.runner_job,
                "kind": "external_launchd_job",
                "serves_job": "C",
                "declared_enabled": True,
                "effective_enabled": bool(launchd.get(spec.label, {}).get("loaded")),
                "launchd": launchd.get(spec.label, {"available": False, "loaded": None}),
                "schedule": spec.schedule_label,
                "schedule_entries": [dict(item) for item in spec.schedule],
                "skips_non_trading_days": spec.skips_non_trading_days,
                "risk_class": spec.risk_class,
                "available_actions": list(spec.allowed_manual_actions),
                "requires_confirmation_actions": list(spec.requires_confirmation_actions),
                "command": ["scripts/launchd/run_bhiksha_job.sh", *spec.runner_args()],
                "logs": {
                    "stdout": str(spec.stdout_log(repo_root)),
                    "stderr": str(spec.stderr_log(repo_root)),
                    "stdout_exists": spec.stdout_log(repo_root).is_file(),
                    "stderr_exists": spec.stderr_log(repo_root).is_file(),
                },
                "last": _last_job_view(latest_record, latest_payload),
            }
        )

    runtime_status = _runtime_status(repo_root=repo_root)
    return {
        "schema": "bhiksha.launchd.status.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "host": os.uname().nodename,
        "repo_root": str(repo_root),
        "active_plan_path": str((repo_root / active_plan_path).resolve() if not active_plan_path.is_absolute() else active_plan_path),
        "latest_status_path": str(latest_status_path(repo_root)),
        "jobs": jobs,
        "runtime": runtime_status,
        "reports": {"latest": _latest_report_summary(repo_root)},
        "schwab_token_guard": {"latest": _latest_schwab_summary(repo_root)},
        "transport": _transport_rollup(jobs),
    }


def _launchd_state() -> dict[str, dict[str, Any]]:
    if shutil.which("launchctl") is None:
        return {spec.label: {"available": False, "loaded": None, "reason": "launchctl_not_found"} for spec in active_launchd_jobs()}
    result: dict[str, dict[str, Any]] = {}
    uid = os.getuid()
    for spec in active_launchd_jobs():
        completed = subprocess.run(  # noqa: S603
            ["launchctl", "print", f"gui/{uid}/{spec.label}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        result[spec.label] = {
            "available": True,
            "loaded": completed.returncode == 0,
            "return_code": completed.returncode,
            "state": _parse_launchctl_field(completed.stdout, "state"),
            "last_exit_code": _parse_launchctl_field(completed.stdout, "last exit code"),
            "stderr_tail": _tail(completed.stderr),
        }
    return result


def _parse_launchctl_field(text: str, key: str) -> str | None:
    prefix = f"{key} ="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split("=", 1)[1].strip()
    return None


def _latest_log_payload(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.startswith("BHIKSHA_LAUNCHD_JOB="):
            continue
        try:
            parsed = json.loads(line.split("=", 1)[1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _last_job_view(latest_record: Any, latest_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if latest_payload is None:
        return None
    alert = _extract_alert(latest_payload)
    return {
        "recorded_at": latest_record.get("recorded_at") if isinstance(latest_record, dict) else None,
        "status": latest_payload.get("status"),
        "domain": _domain_health(latest_payload),
        "transport": _transport_health(alert),
        "alert": _alert_summary(alert),
        "action_id": latest_payload.get("action_id"),
        "payload": latest_payload,
    }


def _domain_health(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("job") == "session-report":
        status = str(payload.get("report_status") or "").upper()
        return {"ok": payload.get("status") == "ok", "status": status or payload.get("status"), "reason": "session_report"}
    if payload.get("job") == "schwab-refresh":
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        final = result.get("final") if isinstance(result.get("final"), dict) else {}
        return {"ok": bool(result.get("ok")), "status": final.get("state"), "reason": "schwab_token_guard"}
    return {"ok": payload.get("status") == "ok", "status": payload.get("status"), "reason": payload.get("reason")}


def _transport_health(alert: dict[str, Any] | None) -> dict[str, Any]:
    if not alert:
        return {"attempted": False, "ok": None, "status": "not_attempted"}
    attempted = bool(alert.get("attempted"))
    ok = alert.get("ok")
    if not attempted:
        status = "not_attempted"
    elif ok is True:
        status = "delivered"
    else:
        status = "degraded"
    return {
        "attempted": attempted,
        "ok": ok if isinstance(ok, bool) else None,
        "status": status,
        "mode": alert.get("mode"),
        "network_call_performed": alert.get("network_call_performed"),
        "return_code": alert.get("return_code"),
    }


def _extract_alert(payload: dict[str, Any]) -> dict[str, Any] | None:
    alert = payload.get("alert")
    if isinstance(alert, dict):
        return alert
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("alert"), dict):
        return result["alert"]
    return None


def _alert_summary(alert: dict[str, Any] | None) -> dict[str, Any] | None:
    if not alert:
        return None
    return {
        "attempted": alert.get("attempted"),
        "ok": alert.get("ok"),
        "mode": alert.get("mode"),
        "return_code": alert.get("return_code"),
        "live_send_requested": alert.get("live_send_requested"),
        "network_call_performed": alert.get("network_call_performed"),
        "error": alert.get("error"),
    }


def _runtime_status(*, repo_root: Path) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603
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
    payload = None
    for line in completed.stdout.splitlines():
        if line.startswith("RUNTIME_STATUS="):
            try:
                parsed = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                parsed = None
            payload = parsed if isinstance(parsed, dict) else None
    return {
        "ok": completed.returncode == 0,
        "return_code": completed.returncode,
        "status": payload,
        "stderr_tail": _tail(completed.stderr),
    }


def _latest_report_summary(repo_root: Path) -> dict[str, Any] | None:
    reports = sorted((repo_root / "artifacts" / "playbook" / "reports").glob("trade_session_report_*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    path = reports[-1]
    payload = _read_json(path)
    return {
        "path": str(path),
        "trading_date": payload.get("trading_date"),
        "status": payload.get("status"),
        "trade_summary": payload.get("trade_summary"),
    }


def _latest_schwab_summary(repo_root: Path) -> dict[str, Any] | None:
    path = repo_root / "artifacts" / "playbook" / "schwab_token_guard" / "latest.json"
    payload = _read_json(path)
    if not payload:
        return None
    return {
        "path": str(path),
        "ok": payload.get("ok"),
        "checked_at": payload.get("checked_at"),
        "action": payload.get("action"),
        "initial": payload.get("initial"),
        "final": payload.get("final"),
        "alert": _alert_summary(payload.get("alert") if isinstance(payload.get("alert"), dict) else None),
    }


def _transport_rollup(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    degraded = []
    for job in jobs:
        last = job.get("last") or {}
        transport = last.get("transport") or {}
        if transport.get("status") == "degraded":
            degraded.append({"label": job.get("label"), "runner_job": job.get("runner_job"), "transport": transport})
    return {"status": "degraded" if degraded else "ok", "degraded": degraded}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tail(text: str, *, max_lines: int = 12) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


if __name__ == "__main__":
    raise SystemExit(main())
