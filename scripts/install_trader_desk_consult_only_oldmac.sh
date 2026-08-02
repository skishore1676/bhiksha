#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LAUNCHD_DIR="${BHIKSHA_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${BHIKSHA_CONSULT_LOG_DIR:-$REPO_ROOT/artifacts/playbook/consultation_service}"
LABEL="com.bhiksha.trader-desk-consult"
PLIST="$LAUNCHD_DIR/$LABEL.plist"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    mkdir -p "$LAUNCHD_DIR" "$LOG_DIR"
    python3 - "$PLIST" "$REPO_ROOT" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
repo = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
payload = {
    "Label": "com.bhiksha.trader-desk-consult",
    "ProgramArguments": [
        "/bin/bash",
        str(repo / "scripts" / "start_trader_desk_consult_only.sh"),
    ],
    "WorkingDirectory": str(repo),
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "StandardOutPath": str(log_dir / "service.out.log"),
    "StandardErrorPath": str(log_dir / "service.err.log"),
}
plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
plist_path.chmod(0o600)
PY
    uid="$(id -u)"
    launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$uid" "$PLIST"
    launchctl enable "gui/$uid/$LABEL"
    echo "LOADED $LABEL"
    ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL"
    ;;
  uninstall)
    uid="$(id -u)"
    launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
    if [[ -f "$PLIST" ]]; then
      mv "$PLIST" "$PLIST.disabled.$(date +%Y%m%dT%H%M%S)"
    fi
    echo "UNLOADED $LABEL"
    ;;
  *)
    echo "usage: $0 [install|status|uninstall]" >&2
    exit 2
    ;;
esac
