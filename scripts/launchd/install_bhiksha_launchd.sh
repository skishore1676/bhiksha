#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LAUNCHD_DIR="${BHIKSHA_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${BHIKSHA_LAUNCHD_LOG_DIR:-$REPO_ROOT/artifacts/playbook/launchd}"
ACTION="${1:-install}"

mkdir -p "$LAUNCHD_DIR" "$LOG_DIR"

case "$ACTION" in
  install|"")
    python3 - "$REPO_ROOT" "$LAUNCHD_DIR" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

repo = Path(sys.argv[1])
launchd_dir = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
runner = repo / "scripts" / "launchd" / "run_bhiksha_job.sh"


def weekdays(hour, minute):
    return [{"Weekday": day, "Hour": hour, "Minute": minute} for day in range(1, 6)]


def every_10_minutes(start_hour, start_minute, end_hour, end_minute):
    entries = []
    hour, minute = start_hour, start_minute
    while (hour, minute) <= (end_hour, end_minute):
        entries.extend(weekdays(hour, minute))
        minute += 10
        if minute >= 60:
            hour += 1
            minute -= 60
    return entries


jobs = [
    {
        "label": "com.bhiksha.live-start",
        "args": ["live-start"],
        "schedule": weekdays(8, 20),
    },
    {
        "label": "com.bhiksha.live-watchdog",
        "args": ["live-watchdog"],
        "schedule": every_10_minutes(8, 30, 15, 0),
    },
    {
        "label": "com.bhiksha.live-stop",
        "args": ["live-stop"],
        "schedule": weekdays(15, 10),
    },
    {
        "label": "com.bhiksha.schwab-guard",
        "args": ["schwab-refresh"],
        "schedule": weekdays(7, 10),
    },
    {
        "label": "com.bhiksha.session-report",
        "args": ["session-report"],
        "schedule": (
            weekdays(9, 45)
            + weekdays(12, 15)
            + weekdays(15, 8)
        ),
    },
]

for job in jobs:
    label = job["label"]
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(runner), *job["args"]],
        "StartCalendarInterval": job["schedule"],
        "WorkingDirectory": str(repo),
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
    }
    path = launchd_dir / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    path.chmod(0o600)
    print(f"WROTE {path}")
PY
    uid="$(id -u)"
    for label in \
      com.bhiksha.live-start \
      com.bhiksha.live-watchdog \
      com.bhiksha.live-stop \
      com.bhiksha.schwab-guard \
      com.bhiksha.session-report
    do
      plist="$LAUNCHD_DIR/$label.plist"
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$uid" "$plist"
      launchctl enable "gui/$uid/$label"
      echo "LOADED $label"
    done
    ;;
  uninstall)
    uid="$(id -u)"
    for label in \
      com.bhiksha.live-start \
      com.bhiksha.live-watchdog \
      com.bhiksha.live-stop \
      com.bhiksha.schwab-guard \
      com.bhiksha.session-report
    do
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$label.plist"
      echo "UNLOADED $label"
    done
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
