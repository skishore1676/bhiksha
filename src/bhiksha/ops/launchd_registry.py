"""Bhiksha-owned launchd job registry.

This registry is the contract between Bhiksha's scheduler, status snapshot, and
the external Control Tower adapter. Lathi may display or invoke these jobs, but
the command shape and safety metadata stay owned here.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ScheduleEntry = dict[str, int]


@dataclass(frozen=True, slots=True)
class LaunchdJobSpec:
    label: str
    runner_job: str
    schedule: tuple[ScheduleEntry, ...]
    schedule_label: str
    purpose: str
    skips_non_trading_days: bool
    risk_class: str
    allowed_manual_actions: tuple[str, ...] = ()
    requires_confirmation_actions: tuple[str, ...] = ()
    install_opt_in_env: str | None = None

    def install_enabled(self) -> bool:
        if self.install_opt_in_env is None:
            return True
        return os.getenv(self.install_opt_in_env, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def runner_args(self) -> list[str]:
        return [self.runner_job]

    def stdout_log(self, repo_root: Path) -> Path:
        return self.log_dir(repo_root) / f"{self.label}.out.log"

    def stderr_log(self, repo_root: Path) -> Path:
        return self.log_dir(repo_root) / f"{self.label}.err.log"

    def log_dir(self, repo_root: Path) -> Path:
        return _log_dir(repo_root)

    def to_dict(self, *, repo_root: Path | None = None) -> dict[str, Any]:
        payload = asdict(self)
        payload["schedule"] = [dict(item) for item in self.schedule]
        payload["runner_args"] = self.runner_args()
        if repo_root is not None:
            payload["stdout_log"] = str(self.stdout_log(repo_root))
            payload["stderr_log"] = str(self.stderr_log(repo_root))
        return payload


def weekdays(hour: int, minute: int) -> tuple[ScheduleEntry, ...]:
    return tuple({"Weekday": day, "Hour": hour, "Minute": minute} for day in range(1, 6))


def fridays(hour: int, minute: int) -> tuple[ScheduleEntry, ...]:
    return ({"Weekday": 5, "Hour": hour, "Minute": minute},)


def every_10_minutes(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> tuple[ScheduleEntry, ...]:
    entries: list[ScheduleEntry] = []
    hour, minute = start_hour, start_minute
    while (hour, minute) <= (end_hour, end_minute):
        entries.extend(weekdays(hour, minute))
        minute += 10
        if minute >= 60:
            hour += 1
            minute -= 60
    return tuple(entries)


ACTIVE_LAUNCHD_JOBS: tuple[LaunchdJobSpec, ...] = (
    LaunchdJobSpec(
        label="com.bhiksha.live-start",
        runner_job="live-start",
        schedule=weekdays(8, 20),
        schedule_label="Weekdays 08:20 CT",
        purpose="Restart Bhiksha live runtime from the active plan.",
        skips_non_trading_days=True,
        risk_class="trading_runtime",
        requires_confirmation_actions=("restart-live-runtime",),
    ),
    LaunchdJobSpec(
        label="com.bhiksha.live-watchdog",
        runner_job="live-watchdog",
        schedule=every_10_minutes(8, 30, 15, 0),
        schedule_label="Weekdays every 10 minutes from 08:30 through 15:00 CT",
        purpose="Ensure the live runtime is still running.",
        skips_non_trading_days=True,
        risk_class="trading_runtime",
        allowed_manual_actions=("ensure-live-runtime",),
        requires_confirmation_actions=("ensure-live-runtime",),
    ),
    LaunchdJobSpec(
        label="com.bhiksha.reconciliation-supervisor",
        runner_job="reconciliation-supervisor",
        schedule=every_10_minutes(8, 30, 15, 0),
        schedule_label="Weekdays every 10 minutes from 08:30 through 15:00 CT",
        purpose="Verify entry reconciliation self-heals and escalate only stale, unresolved holds.",
        skips_non_trading_days=True,
        risk_class="trading_safety_observer",
    ),
    LaunchdJobSpec(
        label="com.bhiksha.live-stop",
        runner_job="live-stop",
        schedule=weekdays(15, 10),
        schedule_label="Weekdays 15:10 CT",
        purpose="Stop the live runtime so stale processes do not survive after close.",
        skips_non_trading_days=False,
        risk_class="trading_runtime",
        requires_confirmation_actions=("stop-live-runtime",),
    ),
    LaunchdJobSpec(
        label="com.bhiksha.schwab-guard",
        runner_job="schwab-refresh",
        schedule=weekdays(7, 10) + weekdays(15, 20),
        schedule_label="Trading days 07:10 and 15:20 CT",
        purpose="Verify premarket auth and renew after close before the next trading session.",
        skips_non_trading_days=True,
        risk_class="auth_maintenance",
        allowed_manual_actions=("schwab-guard-now", "renew-schwab-access"),
        requires_confirmation_actions=("renew-schwab-access",),
    ),
    LaunchdJobSpec(
        label="com.bhiksha.session-report",
        runner_job="session-report",
        schedule=weekdays(9, 10) + weekdays(11, 45) + weekdays(14, 45),
        schedule_label="Weekdays 09:10, 11:45, and 14:45 CT",
        purpose="Send an intraday session report early enough for manual action.",
        skips_non_trading_days=True,
        risk_class="operator_report",
        allowed_manual_actions=("session-report-now",),
    ),
    LaunchdJobSpec(
        label="com.bhiksha.weekly-trading-decisions",
        runner_job="weekly-trading-decisions",
        schedule=fridays(16, 0),
        schedule_label="Fridays 16:00 CT (after market close)",
        purpose="Produce the workbook-backed evidence packet consumed by TradeLab governance review.",
        skips_non_trading_days=False,
        risk_class="operator_report",
        allowed_manual_actions=("weekly-trading-decisions-now",),
    ),
)


def active_launchd_jobs() -> tuple[LaunchdJobSpec, ...]:
    return tuple(job for job in ACTIVE_LAUNCHD_JOBS if job.install_enabled())


def registered_launchd_jobs() -> tuple[LaunchdJobSpec, ...]:
    return ACTIVE_LAUNCHD_JOBS


def job_by_runner(runner_job: str) -> LaunchdJobSpec | None:
    for job in ACTIVE_LAUNCHD_JOBS:
        if job.runner_job == runner_job:
            return job
    return None


def job_by_label(label: str) -> LaunchdJobSpec | None:
    for job in ACTIVE_LAUNCHD_JOBS:
        if job.label == label:
            return job
    return None


def labels() -> tuple[str, ...]:
    return tuple(job.label for job in ACTIVE_LAUNCHD_JOBS)


def launchd_log_dir(repo_root: Path) -> Path:
    return _log_dir(repo_root)


def latest_status_path(repo_root: Path) -> Path:
    return _log_dir(repo_root) / "latest_status.json"


def control_lock_dir(repo_root: Path) -> Path:
    return _log_dir(repo_root) / "control_locks"


def _log_dir(repo_root: Path) -> Path:
    return repo_root / "artifacts" / "playbook" / "launchd"
