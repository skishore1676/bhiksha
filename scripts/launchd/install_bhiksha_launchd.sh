#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LAUNCHD_DIR="${BHIKSHA_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${BHIKSHA_LAUNCHD_LOG_DIR:-$REPO_ROOT/artifacts/playbook/launchd}"
RUNTIME_FLAG_DIR="${BHIKSHA_RUNTIME_FLAG_DIR:-$REPO_ROOT/artifacts/playbook/runtime_flags}"
ACTION="${1:-install}"

mkdir -p "$LAUNCHD_DIR" "$LOG_DIR" "$RUNTIME_FLAG_DIR"

case "$ACTION" in
  install|"")
    python3 - "$REPO_ROOT" "$LAUNCHD_DIR" "$LOG_DIR" <<'PY'
import plistlib
import os
import re
import sys
from pathlib import Path

repo = Path(sys.argv[1])
launchd_dir = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
runner = repo / "scripts" / "launchd" / "run_bhiksha_job.sh"
sys.path.insert(0, str(repo / "src"))

from bhiksha.ops.launchd_registry import active_launchd_jobs

exit_edge_enabled = str(
    os.environ.get("BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED", "")
).strip().lower() in {"1", "true", "yes", "on"}
active_plan_id = None
if "BHIKSHA_ACTIVE_PLAN_ID" in os.environ:
    raw_active_plan_id = os.environ["BHIKSHA_ACTIVE_PLAN_ID"]
    active_plan_id = raw_active_plan_id.strip()
    if (
        not active_plan_id
        or active_plan_id != raw_active_plan_id
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            active_plan_id,
        )
        is None
    ):
        raise SystemExit(
            "BHIKSHA_ACTIVE_PLAN_ID must be a nonblank stable id using only "
            "letters, digits, '.', '_', ':', or '-'"
        )

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
    if label in {"com.bhiksha.live-start", "com.bhiksha.live-watchdog"}:
        # Install-time controls become persistent scheduled-context values.
        # Generic installs omit both; an interactive shell export alone never
        # changes an already-installed launchd job.
        environment = {}
        if exit_edge_enabled:
            environment["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED"] = "true"
        if active_plan_id is not None:
            environment["BHIKSHA_ACTIVE_PLAN_ID"] = active_plan_id
        if environment:
            plist["EnvironmentVariables"] = environment
    path = launchd_dir / f"{label}.plist"
    path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    path.chmod(0o600)
    print(f"WROTE {path}")
PY
    case "${BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED:-}" in
      1|true|TRUE|yes|YES|on|ON)
        touch "$RUNTIME_FLAG_DIR/exit_edge_live_shadow.enabled"
        ;;
      *)
        rm -f "$RUNTIME_FLAG_DIR/exit_edge_live_shadow.enabled"
        ;;
    esac
    uid="$(id -u)"
    # These calculators remain available as internal/manual tools, but their
    # duplicate operator-facing schedules were replaced by the single Friday
    # workbook-backed decision review.
    for retired_label in com.bhiksha.weekly-scorecard com.bhiksha.shadow-ev-report
    do
      launchctl bootout "gui/$uid/$retired_label" >/dev/null 2>&1 || true
      rm -f "$LAUNCHD_DIR/$retired_label.plist"
      echo "RETIRED $retired_label"
    done
    for label in \
      com.bhiksha.live-start \
      com.bhiksha.live-watchdog \
      com.bhiksha.reconciliation-supervisor \
      com.bhiksha.live-stop \
      com.bhiksha.schwab-guard \
      com.bhiksha.session-report \
      com.bhiksha.weekly-trading-decisions
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
      com.bhiksha.reconciliation-supervisor \
      com.bhiksha.live-stop \
      com.bhiksha.schwab-guard \
      com.bhiksha.session-report \
      com.bhiksha.weekly-trading-decisions \
      com.bhiksha.weekly-scorecard \
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
