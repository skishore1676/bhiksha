#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LAUNCHD_DIR="${BHIKSHA_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
ACTION="${1:-install}"
INSTALL_PYTHON="${BHIKSHA_PYTHON:-$(command -v python3)}"
LOG_DIR="${BHIKSHA_LAUNCHD_LOG_DIR:-$REPO_ROOT/artifacts/playbook/launchd}"
RUNTIME_FLAG_DIR="${BHIKSHA_RUNTIME_FLAG_DIR:-$REPO_ROOT/artifacts/playbook/runtime_flags}"

"$INSTALL_PYTHON" - "$LAUNCHD_DIR" "$LOG_DIR" "$RUNTIME_FLAG_DIR" <<'PY'
import sys
from pathlib import Path

launchd_dir = Path(sys.argv[1]).expanduser()
log_dir = Path(sys.argv[2]).expanduser()
flag_dir = Path(sys.argv[3]).expanduser()

def reject_symlinks(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise SystemExit(f"{label} must be absolute")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise SystemExit(f"{label} cannot contain a symlink: {candidate}")
    return path.resolve()

launchd = reject_symlinks(launchd_dir, label="launchd directory")
logs = reject_symlinks(log_dir, label="launchd log directory")
flags = reject_symlinks(flag_dir, label="runtime marker directory")
for directory in (launchd, logs, flags):
    directory.mkdir(parents=True, exist_ok=True)
PY

case "$ACTION" in
  install|"")
    "$INSTALL_PYTHON" - "$REPO_ROOT" "$LAUNCHD_DIR" "$LOG_DIR" <<'PY'
import hashlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
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
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", active_plan_id) is None
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
    environment = {}
    if label in {"com.bhiksha.live-start", "com.bhiksha.live-watchdog"}:
        if exit_edge_enabled:
            environment["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED"] = "true"
        if active_plan_id is not None:
            environment["BHIKSHA_ACTIVE_PLAN_ID"] = active_plan_id
    if environment:
        plist["EnvironmentVariables"] = environment
    path = launchd_dir / f"{label}.plist"
    if path.is_symlink():
        raise SystemExit(f"launchd plist cannot be a symlink: {path}")
    for log_path in (Path(plist["StandardOutPath"]), Path(plist["StandardErrorPath"])):
        if log_path.is_symlink():
            raise SystemExit(f"launchd log cannot be a symlink: {log_path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=launchd_dir)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plistlib.dumps(plist, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
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
    rm -f "$RUNTIME_FLAG_DIR/exit_edge_live_shadow.enabled"
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
