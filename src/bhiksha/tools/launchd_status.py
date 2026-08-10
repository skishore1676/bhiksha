"""Emit a Bhiksha launchd/control status snapshot for external observers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bhiksha.config.environment import load_dotenv
from bhiksha.ops.launchd_registry import latest_status_path, registered_launchd_jobs
from bhiksha.ops.provider_reconciliation_health import inspect_provider_reconciliation

# External callers (lathi Control Tower) kill this command at 20s
# (LATHI_BHIKSHA_TIMEOUT_SECONDS). Keep the whole snapshot under that:
# every subprocess probe is bounded, TimeoutExpired degrades only that
# probe's field, and once the overall budget is nearly spent the remaining
# probes short-circuit to "not_checked" instead of running.
_DEFAULT_BUDGET_SECONDS = 15.0
_MIN_PROBE_SECONDS = 1.0
_LAUNCHCTL_PROBE_SECONDS = 5.0
_RUNTIME_PROBE_SECONDS = 10.0
_STALE_SCHEDULE_GRACE_MINUTES = 5


class _Deadline:
    """Wall-clock budget shared across all status probes."""

    def __init__(self, budget_seconds: float) -> None:
        self.budget_seconds = budget_seconds
        self._expires_at = time.monotonic() + budget_seconds

    def remaining(self) -> float:
        return self._expires_at - time.monotonic()

    def probe_timeout(self, preferred: float) -> float | None:
        """Timeout to give the next probe, or None when it must be skipped."""
        remaining = self.remaining()
        if remaining < _MIN_PROBE_SECONDS:
            return None
        return max(_MIN_PROBE_SECONDS, min(preferred, remaining))


def _status_budget_seconds() -> float:
    raw = os.getenv("BHIKSHA_STATUS_BUDGET_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_BUDGET_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BUDGET_SECONDS


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


def build_status_snapshot(
    *,
    repo_root: Path,
    active_plan_path: Path,
    now: datetime | None = None,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    deadline = _Deadline(budget_seconds if budget_seconds is not None else _status_budget_seconds())
    latest = _read_json(latest_status_path(repo_root))
    latest_jobs = latest.get("jobs") if isinstance(latest.get("jobs"), dict) else {}
    launchd = _launchd_state(deadline=deadline)
    jobs = []
    generated_at = now or datetime.now(UTC)
    provider_reconciliation = inspect_provider_reconciliation(repo_root / "bhiksha.db")
    for spec in registered_launchd_jobs():
        latest_record = latest_jobs.get(spec.runner_job) if isinstance(latest_jobs, dict) else None
        latest_payload = (latest_record or {}).get("payload") if isinstance(latest_record, dict) else None
        if not isinstance(latest_payload, dict):
            latest_payload = _latest_log_payload(spec.stdout_log(repo_root))
            if isinstance(latest_payload, dict) and not isinstance(latest_record, dict):
                latest_record = {"recorded_at": _file_mtime_iso(spec.stdout_log(repo_root))}
        last = _last_job_view(latest_record, latest_payload)
        if spec.runner_job == "session-report":
            _apply_current_provider_health(last, provider_reconciliation)
        findings = _job_findings(last)
        findings = list(findings)
        findings.extend(_launchd_exit_findings(spec, launchd.get(spec.label, {}), last, now=generated_at))
        # Stale detection for launchd jobs that crash before writing latest_status —
        # latest_status stays frozen at the last good run, so domain stays ok.
        findings.extend(_stale_last_run_findings(spec, last, now=generated_at))
        # Deduplicate while preserving order
        seen: set[str] = set()
        findings = [item for item in findings if not (item in seen or seen.add(item))]  # type: ignore[func-returns-value]
        job = {
                "label": spec.label,
                "title": _job_title(spec.runner_job, spec.label),
                "runner_job": spec.runner_job,
                "kind": "external_launchd_job",
                "serves_job": "C",
                "declared_enabled": _declared_enabled(spec, repo_root=repo_root),
                "effective_enabled": bool(launchd.get(spec.label, {}).get("loaded")),
                "launchd": launchd.get(spec.label, {"available": False, "loaded": None}),
                "schedule": spec.schedule_label,
                "schedule_entries": [dict(item) for item in spec.schedule],
                "next_fire": _next_fire(spec.schedule, now=generated_at),
                "skips_non_trading_days": spec.skips_non_trading_days,
                "risk_class": spec.risk_class,
                "available_actions": list(spec.allowed_manual_actions),
                "requires_confirmation_actions": list(spec.requires_confirmation_actions),
                "action_requirements": _action_requirements(spec.requires_confirmation_actions),
                "command": ["scripts/launchd/run_bhiksha_job.sh", *spec.runner_args()],
                "logs": {
                    "stdout": str(spec.stdout_log(repo_root)),
                    "stderr": str(spec.stderr_log(repo_root)),
                    "stdout_exists": spec.stdout_log(repo_root).is_file(),
                    "stderr_exists": spec.stderr_log(repo_root).is_file(),
                },
                "last": last,
                "last_run_status": _last_run_status(last),
                "last_run_at": last.get("recorded_at") if isinstance(last, dict) else None,
                "transport_status": _last_transport_status(last),
                "findings": findings,
                "lifecycle": (
                    "armed"
                    if not findings
                    else "waiting_you"
                    if findings and spec.runner_job in {"schwab-refresh", "reconciliation-supervisor"}
                    else "armed"
                    if not spec.install_enabled() or not bool(launchd.get(spec.label, {}).get("loaded"))
                    else "stuck"
                    if findings and any("launchd job failed" in f or "stale_last_run" in f for f in findings)
                    else "waiting_you"
                    if findings
                    else None
                ),
            }
        details = _job_details(last)
        if details:
            job["details"] = details
        if spec.runner_job == "reconciliation-supervisor" and isinstance(last, dict):
            supervision = _reconciliation_payload(last)
            if supervision:
                job["summary"] = _reconciliation_summary(supervision)
        jobs.append(job)

    runtime_status = _runtime_status(repo_root=repo_root, deadline=deadline)
    _apply_live_start_recovery(jobs, runtime_status)
    return {
        "schema": "bhiksha.launchd.status.v1",
        "generated_at": generated_at.isoformat(),
        "host": os.uname().nodename,
        "repo_root": str(repo_root),
        "active_plan_path": str((repo_root / active_plan_path).resolve() if not active_plan_path.is_absolute() else active_plan_path),
        "latest_status_path": str(latest_status_path(repo_root)),
        "jobs": jobs,
        "runtime": runtime_status,
        "reports": {"latest": _latest_report_summary(repo_root)},
        "schwab_token_guard": {"latest": _latest_schwab_summary(repo_root)},
        "provider_reconciliation": provider_reconciliation,
        "transport": _transport_rollup(jobs),
    }


def _apply_live_start_recovery(jobs: list[dict[str, Any]], runtime_status: dict[str, Any]) -> None:
    """Clear current start attention only after a later watchdog recovery.

    The failed scheduled start remains the job's historical last-run result.
    Current health becomes armed only when the runtime started after that
    failure and a still-later watchdog run confirmed it healthy. After the
    session boundary, a successful stop receipt for that same PID preserves
    the recovery proof without pretending the runtime is still running.
    """

    runtime = runtime_status.get("status") if isinstance(runtime_status, dict) else None
    if runtime_status.get("ok") is not True or not isinstance(runtime, dict):
        return

    by_runner = {str(job.get("runner_job") or ""): job for job in jobs}
    live_start = by_runner.get("live-start")
    watchdog = by_runner.get("live-watchdog")
    if live_start is None or watchdog is None:
        return

    start_domain = _job_domain(live_start)
    watchdog_domain = _job_domain(watchdog)
    if start_domain.get("ok") is not False or watchdog_domain.get("ok") is not True:
        return

    watchdog_runtime = _watchdog_runtime_status(watchdog)
    if watchdog_runtime.get("running") is not True or watchdog_runtime.get("live") is not True:
        return

    failed_at = _parse_timestamp(live_start.get("last_run_at"))
    watchdog_runtime_started_at = _parse_timestamp(watchdog_runtime.get("started_at"))
    watchdog_at = _parse_timestamp(watchdog.get("last_run_at"))
    if (
        failed_at is None
        or watchdog_runtime_started_at is None
        or watchdog_at is None
    ):
        return
    if watchdog_runtime_started_at <= failed_at or watchdog_at < watchdog_runtime_started_at:
        return

    recovery_state: str
    summary: str
    updated_at = str(watchdog.get("last_run_at"))
    if runtime.get("running") is True:
        runtime_started_at = _parse_timestamp(runtime.get("started_at"))
        if (
            runtime.get("live") is not True
            or watchdog_runtime.get("pid") != runtime.get("pid")
            or runtime_started_at != watchdog_runtime_started_at
        ):
            return
        recovery_state = "running"
        summary = "Recovered by live watchdog; the live runtime is running."
    elif runtime.get("running") is False:
        live_stop = by_runner.get("live-stop")
        stop_domain = _job_domain(live_stop) if live_stop is not None else {}
        stop_runtime = _watchdog_runtime_status(live_stop) if live_stop is not None else {}
        stop_at = _parse_timestamp(live_stop.get("last_run_at")) if live_stop is not None else None
        if (
            stop_domain.get("ok") is not True
            or stop_runtime.get("action") != "stopped"
            or stop_runtime.get("running") is not False
            or stop_runtime.get("pid") != watchdog_runtime.get("pid")
            or stop_at is None
            or stop_at <= watchdog_at
        ):
            return
        recovery_state = "stopped_cleanly"
        summary = "Recovered by live watchdog; the live runtime later stopped cleanly at session close."
        updated_at = str(live_stop.get("last_run_at"))
    else:
        return

    failure_reason = _historical_failure_reason(live_start)
    live_start["lifecycle"] = "armed"
    live_start["findings"] = []
    live_start["summary"] = summary
    live_start["details"] = [
        {
            "kind": "runtime_recovery",
            "title": "Recovered by live watchdog",
            "surface": "Bhiksha live runtime",
            "status": f"{recovery_state}; prior start failed: {failure_reason}",
            "updated_at": updated_at,
            "review_ref": f"scheduled start failed at {live_start.get('last_run_at')}",
        }
    ]


def _job_domain(job: dict[str, Any]) -> dict[str, Any]:
    last = job.get("last") if isinstance(job.get("last"), dict) else {}
    domain = last.get("domain") if isinstance(last.get("domain"), dict) else {}
    return domain


def _watchdog_runtime_status(job: dict[str, Any]) -> dict[str, Any]:
    last = job.get("last") if isinstance(job.get("last"), dict) else {}
    payload = last.get("payload") if isinstance(last.get("payload"), dict) else {}
    stdout_tail = str(payload.get("stdout_tail") or "")
    for line in reversed(stdout_tail.splitlines()):
        if not line.startswith("RUNTIME_STATUS="):
            continue
        try:
            parsed = json.loads(line.split("=", 1)[1])
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_STARTUP_HEALTH_FAILURE_RE = re.compile(r"Startup health check failed for:\s*([A-Za-z0-9_, -]+)")


def _historical_failure_reason(job: dict[str, Any]) -> str:
    last = job.get("last") if isinstance(job.get("last"), dict) else {}
    payload = last.get("payload") if isinstance(last.get("payload"), dict) else {}
    failure_text = "\n".join(
        str(payload.get(key) or "")
        for key in ("error", "stderr_tail", "stdout_tail")
    )
    match = _STARTUP_HEALTH_FAILURE_RE.search(failure_text)
    if match:
        return f"Startup health check failed for: {match.group(1).strip()}"
    findings = [str(item) for item in job.get("findings") or [] if str(item).strip()]
    return findings[0] if findings else f"status {job.get('last_run_status') or 'failed'}"


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _launchd_state(*, deadline: _Deadline | None = None) -> dict[str, dict[str, Any]]:
    if shutil.which("launchctl") is None:
        return {
            spec.label: {
                "available": False,
                "loaded": None,
                "reason": "launchctl_not_found",
            }
            for spec in registered_launchd_jobs()
        }
    deadline = deadline or _Deadline(_status_budget_seconds())
    result: dict[str, dict[str, Any]] = {}
    uid = os.getuid()
    for spec in registered_launchd_jobs():
        probe_timeout = deadline.probe_timeout(_LAUNCHCTL_PROBE_SECONDS)
        if probe_timeout is None:
            result[spec.label] = _degraded_launchd_probe("not_checked", "status budget exhausted before probe")
            continue
        try:
            completed = subprocess.run(  # noqa: S603
                ["launchctl", "print", f"gui/{uid}/{spec.label}"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=probe_timeout,
            )
        except subprocess.TimeoutExpired:
            result[spec.label] = _degraded_launchd_probe("timeout", f"launchctl print exceeded {probe_timeout:.1f}s")
            continue
        except (subprocess.SubprocessError, OSError) as exc:
            result[spec.label] = _degraded_launchd_probe("error", str(exc))
            continue
        result[spec.label] = {
            "available": True,
            "loaded": completed.returncode == 0,
            "return_code": completed.returncode,
            "state": _parse_launchctl_field(completed.stdout, "state"),
            "last_exit_code": _parse_launchctl_field(completed.stdout, "last exit code"),
            "stderr_tail": _tail(completed.stderr),
        }
    return result


def _degraded_launchd_probe(state: str, detail: str) -> dict[str, Any]:
    """Same shape as a successful launchctl probe; `state` carries the degraded value."""
    return {
        "available": True,
        "loaded": None,
        "return_code": None,
        "state": state,
        "last_exit_code": None,
        "stderr_tail": detail,
    }


def _parse_launchctl_field(text: str, key: str) -> str | None:
    prefix = f"{key} ="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split("=", 1)[1].strip()
    return None


def _declared_enabled(spec: Any, *, repo_root: Path) -> bool:
    if spec.install_opt_in_env is None:
        return True
    if spec.runner_job == "chart-scenario-shadow":
        return (
            repo_root
            / "artifacts"
            / "playbook"
            / "runtime_flags"
            / "chart_scenario_shadow.enabled"
        ).is_file()
    return spec.install_enabled()


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


def _file_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
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


def _apply_current_provider_health(
    last: dict[str, Any] | None,
    provider_reconciliation: dict[str, Any],
) -> None:
    """Clear a historical provider report only after durable success evidence."""

    if not isinstance(last, dict) or provider_reconciliation.get("state") != "recovered":
        return
    domain = last.get("domain") if isinstance(last.get("domain"), dict) else {}
    if domain.get("report_reason") not in {
        "provider_warning",
        "degraded_reconciliation",
        "blocking_reconciliation_failure",
        "reconciliation_recovery_exhausted",
    }:
        return
    domain.update(
        {
            "ok": True,
            "status": "recovered",
            "attention_required": False,
            "recovery_state": "recovered",
            "recovered_at": (provider_reconciliation.get("last_recovery") or {}).get("created_at"),
        }
    )
    last["domain"] = domain


def _last_run_status(last: dict[str, Any] | None) -> str | None:
    if not isinstance(last, dict):
        return None
    domain = last.get("domain") if isinstance(last.get("domain"), dict) else {}
    return domain.get("status") or last.get("status")


def _last_transport_status(last: dict[str, Any] | None) -> str | None:
    if not isinstance(last, dict):
        return None
    transport = last.get("transport") if isinstance(last.get("transport"), dict) else {}
    return transport.get("status")


def _action_requirements(actions: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    requirements: dict[str, dict[str, Any]] = {}
    for action in actions:
        if action == "renew-schwab-access":
            requirements[action] = {
                "requires_confirmation": True,
                "label": "Renew Schwab access",
                "reason": "Confirm a Schwab OAuth grant for market-data access. This action never places orders.",
                "owner_confirmation_args": ["--confirm"],
            }
        else:
            requirements[action] = {
                "requires_confirmation": True,
                "reason": "Confirm because this action can affect the live trading runtime.",
            }
    return requirements


def _job_findings(last: dict[str, Any] | None) -> list[str]:
    if not isinstance(last, dict):
        return []
    domain = last.get("domain") if isinstance(last.get("domain"), dict) else {}
    if domain.get("attention_required") is False:
        return []
    if domain.get("ok") is not False:
        return []
    failure_kind = str(domain.get("failure_kind") or "")
    status = str(domain.get("status") or "unknown")
    messages = {
        "schwab_authentication_expired": "Schwab authentication expired; renewal is required.",
        "schwab_authentication_renewal_required": "Schwab authentication will not survive the next trading session.",
        "schwab_authentication_unavailable": "Schwab authentication is unavailable; renewal is required.",
        "browser_renewal_failed": "Automatic Schwab authentication renewal failed.",
        "schwab_access_refresh_failed": "Schwab access-token refresh failed.",
    }
    if failure_kind in messages:
        return [messages[failure_kind]]
    if status == "refresh_token_expired":
        return ["Schwab authentication expired; renewal is required."]
    if status == "refresh_token_near_expiry":
        return ["Schwab authentication will not survive the next trading session."]
    if status == "needs_human":
        return ["Entry reconciliation could not finish safely; the affected deployment remains blocked."]
    return [f"Domain health failed: {status}"]


def _launchd_exit_findings(
    spec: Any,
    launchd: dict[str, Any],
    last: dict[str, Any] | None,
    *,
    now: datetime,
) -> list[str]:
    """Surface a launchd non-zero exit as a finding when domain still says ok.

    Historically Bhiksha only surfaced domain-level failures. When
    launchd_job crashes at import (e.g. missing dep after a deploy), it never
    writes latest_status, so the status file stays frozen at the last good
    run and the Control Tower shows green. The launchd last_exit_code is
    collected but never promoted. This helper bridges that gap.
    """
    code = str(launchd.get("last_exit_code") or "").strip()
    if code in ("", "0", "-", "(never exited)"):
        return []
    # launchd loaded but last exit non-zero and we have no fresh last_run_at
    # beyond the failure window — report it.
    # For initial fix, any non-zero exit produces a finding; lifecycle
    # handling below will surface it to Lathi.
    label = spec.label if hasattr(spec, "label") else str(spec)
    log_hint = spec.stderr_log(Path.cwd()).name if hasattr(spec, "stderr_log") else f"{label}.err.log"
    return [f"launchd job failed: {label} last exit {code} — check {log_hint} and latest_status freshness"]


def _stale_last_run_findings(
    spec: Any,
    last: dict[str, Any] | None,
    *,
    now: datetime,
) -> list[str]:
    """Detect stale latest_status after a scheduled fire is actually due.

    ``latest_status`` intentionally remains frozen when a launchd job skips a
    weekend or holiday. Compare it with the latest Central-time scheduled fire,
    rather than with elapsed wall-clock age from the previous session.
    """
    from bhiksha.market_data.trading_calendar import CENTRAL, is_trading_day

    last_at_raw = last.get("recorded_at") if isinstance(last, dict) else None
    last_at = _parse_timestamp(last_at_raw) if last_at_raw else None
    local_now = now.astimezone(CENTRAL)
    skips_non_trading_days = bool(getattr(spec, "skips_non_trading_days", False))
    if skips_non_trading_days and not is_trading_day(local_now.date()):
        return []

    schedule = getattr(spec, "schedule", ())
    due_at = _latest_scheduled_fire(
        schedule,
        now=now,
        skips_non_trading_days=skips_non_trading_days,
    )
    if due_at is None or local_now < due_at + timedelta(
        minutes=_STALE_SCHEDULE_GRACE_MINUTES
    ):
        return []
    if last_at is None and due_at.date() != local_now.date():
        return []
    if last_at is not None and last_at.astimezone(CENTRAL) >= due_at:
        return []

    if last_at is None:
        return [
            "stale_last_run — no successful run recorded; "
            f"expected after {due_at.isoformat()}"
        ]
    age_hours = (now.astimezone(UTC) - last_at.astimezone(UTC)).total_seconds() / 3600.0
    if age_hours >= 0:
        return [
            f"stale_last_run — last ok {last_at_raw} {age_hours:.1f}h ago; "
            f"scheduled fire due at {due_at.isoformat()}"
        ]
    return []


def _latest_scheduled_fire(
    schedule: Any,
    *,
    now: datetime,
    skips_non_trading_days: bool,
) -> datetime | None:
    """Return the latest schedule entry due before ``now``, in Central time."""
    from bhiksha.market_data.trading_calendar import CENTRAL, is_trading_day

    local_now = now.astimezone(CENTRAL)
    candidates: list[datetime] = []
    for offset in range(14):
        day = local_now.date() - timedelta(days=offset)
        if skips_non_trading_days and not is_trading_day(day):
            continue
        weekday = day.weekday() + 1
        for entry in schedule or ():
            if int(entry.get("Weekday", -1)) != weekday:
                continue
            candidate = datetime(
                day.year,
                day.month,
                day.day,
                int(entry.get("Hour", 0)),
                int(entry.get("Minute", 0)),
                tzinfo=CENTRAL,
            )
            if candidate <= local_now:
                candidates.append(candidate)
    return max(candidates) if candidates else None


def _job_title(runner_job: str, label: str) -> str:
    titles = {
        "schwab-refresh": "Schwab authentication",
        "reconciliation-supervisor": "Reconciliation supervision",
    }
    return titles.get(runner_job, label)


def _reconciliation_payload(last: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(last, dict):
        return {}
    payload = last.get("payload") if isinstance(last.get("payload"), dict) else {}
    supervision = payload.get("reconciliation_supervision")
    return supervision if isinstance(supervision, dict) else {}


def _reconciliation_summary(supervision: dict[str, Any]) -> str:
    if supervision.get("attention_required"):
        provider = supervision.get("provider_reconciliation") or {}
        if provider.get("attention_required"):
            return (
                "Needs you: Public portfolio reconciliation exhausted automatic recovery; "
                "live entries remain fail-closed."
            )
        return (
            f"Needs you: {supervision.get('needs_human_count', 0)} entry reconciliation "
            "hold(s) remain unresolved; affected deployments are fail-closed."
        )
    if supervision.get("self_healing_count"):
        return f"Self-healing: {supervision.get('self_healing_count')} transient entry hold(s)."
    provider = supervision.get("provider_reconciliation") or {}
    if provider.get("state") == "self_healing":
        return "Self-healing: Public portfolio reconciliation is inside its automatic recovery window."
    if provider.get("state") == "recovered":
        return "Recovered: the latest Public portfolio reconciliation succeeded without operator action."
    return "Healthy: no unresolved entry reconciliation holds."


def _job_details(last: dict[str, Any] | None) -> list[dict[str, Any]]:
    supervision = _reconciliation_payload(last)
    if not supervision:
        return []
    details: list[dict[str, Any]] = []
    provider = supervision.get("provider_reconciliation") or {}
    if provider.get("state") in {"self_healing", "needs_human", "recovered"}:
        evidence = provider.get("last_failure") or provider.get("last_recovery") or {}
        details.append(
            {
                "kind": "provider_reconciliation",
                "title": "Public portfolio reconciliation",
                "surface": "Bhiksha live runtime",
                "status": provider.get("state"),
                "updated_at": evidence.get("created_at") or provider.get("observed_at"),
                "review_ref": evidence.get("error") or evidence.get("action"),
            }
        )
    for hold in supervision.get("active_holds") or []:
        details.append(
            {
                "kind": "entry_reconciliation_hold",
                "title": f"{hold.get('symbol')} entry reconciliation",
                "surface": hold.get("deployment_id"),
                "status": hold.get("state"),
                "updated_at": supervision.get("observed_at"),
                "review_ref": hold.get("entry_order_id"),
            }
        )
    return details


def _next_fire(schedule: tuple[dict[str, int], ...], *, now: datetime) -> str | None:
    """Return the next scheduled launchd fire time in Central time.

    The Bhiksha registry uses launchd-style weekday numbers where Monday-Friday
    are 1-5.
    """

    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python 3.11 has zoneinfo.
        return None
    central = ZoneInfo("America/Chicago")
    local_now = now.astimezone(central)
    candidates: list[datetime] = []
    for offset in range(8):
        day = local_now.date() + timedelta(days=offset)
        weekday = day.weekday() + 1
        for entry in schedule:
            if int(entry.get("Weekday", -1)) != weekday:
                continue
            candidate = datetime(
                day.year,
                day.month,
                day.day,
                int(entry.get("Hour", 0)),
                int(entry.get("Minute", 0)),
                tzinfo=central,
            )
            if candidate > local_now:
                candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates).isoformat()


def _domain_health(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("job") == "session-report":
        report_status = payload.get("report_status")
        if isinstance(report_status, dict):
            status = str(report_status.get("level") or report_status.get("LEVEL") or "").upper()
            attention_required = bool(report_status.get("attention_required", status == "RED"))
            ok = payload.get("status") == "ok" and not attention_required
        else:
            status = str(report_status or "").upper()
            attention_required = status == "RED"
            ok = payload.get("status") == "ok"
        return {
            "ok": ok,
            "status": "needs_human" if attention_required else status or payload.get("status"),
            "reported_status": status or payload.get("status"),
            "reason": "session_report",
            "report_reason": report_status.get("reason") if isinstance(report_status, dict) else None,
            "attention_required": attention_required,
        }
    if payload.get("job") == "schwab-refresh":
        if payload.get("status") == "skipped":
            return {"ok": True, "status": "skipped", "reason": payload.get("reason") or "non_trading_day"}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        final = result.get("final") if isinstance(result.get("final"), dict) else {}
        return {
            "ok": bool(result.get("ok")),
            "status": final.get("state"),
            "reason": "schwab_token_guard",
            "attention_required": bool(result.get("attention_required", not result.get("ok"))),
            "failure_kind": result.get("failure_kind"),
        }
    if payload.get("job") == "reconciliation-supervisor":
        supervision = payload.get("reconciliation_supervision") if isinstance(
            payload.get("reconciliation_supervision"), dict
        ) else {}
        attention_required = bool(supervision.get("attention_required"))
        return {
            "ok": payload.get("status") == "ok" and not attention_required,
            "status": "needs_human" if attention_required else supervision.get("state") or payload.get("status"),
            "reason": "entry_reconciliation_supervision",
            "attention_required": attention_required,
        }
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


def _runtime_status(*, repo_root: Path, deadline: _Deadline | None = None) -> dict[str, Any]:
    deadline = deadline or _Deadline(_status_budget_seconds())
    probe_timeout = deadline.probe_timeout(_RUNTIME_PROBE_SECONDS)
    if probe_timeout is None:
        return {
            "ok": False,
            "return_code": None,
            "status": None,
            "stderr_tail": "not_checked: status budget exhausted before server_session probe",
        }
    try:
        completed = subprocess.run(  # noqa: S603
            [_bhiksha_python(repo_root), "-m", "bhiksha.tools.server_session", "status"],
            check=False,
            cwd=str(repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=probe_timeout,
            env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "return_code": None,
            "status": None,
            "stderr_tail": f"timeout: server_session status exceeded {probe_timeout:.1f}s",
        }
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False,
            "return_code": None,
            "status": None,
            "stderr_tail": f"error: {exc}",
        }
    # 2026-07-02 operator-audit fix: this parsing block had been stranded after
    # _bhiksha_python()'s return (dead code), so _runtime_status silently
    # returned None and Control Tower / launchd_status reported no runtime
    # state at all — a lie by omission.
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


def _bhiksha_python(repo_root: Path) -> str:
    candidate = repo_root / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return sys.executable


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
