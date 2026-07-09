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
sys.path.insert(0, str(repo / "src"))

from bhiksha.ops.launchd_registry import active_launchd_jobs

for job in active_launchd_jobs():
    label = job.label
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(runner), *job.runner_args()],
        "StartCalendarInterval": [dict(item) for item in job.schedule],
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
      com.bhiksha.session-report \
      com.bhiksha.shadow-ev-report
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
      com.bhiksha.session-report \
      com.bhiksha.shadow-ev-report
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
