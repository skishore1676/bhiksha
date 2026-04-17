#!/bin/bash
set -euo pipefail

REPO_ROOT="/Users/sunny/Documents/bhiksha"
cd "$REPO_ROOT"
ulimit -n 10240
export PYTHONPATH=src
export PYTHONUNBUFFERED=1

RUNTIME_DIR="artifacts/playbook/runtime"
PID_PATH="$RUNTIME_DIR/bhiksha.pid"
LOG_PATH="$RUNTIME_DIR/cron_output.log"
CHILD_PID=""

mkdir -p "$RUNTIME_DIR"

cleanup_pid() {
  local status=$?
  trap - EXIT INT TERM

  if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi

  if [[ -n "$CHILD_PID" ]]; then
    .venv/bin/python - "$PID_PATH" "$CHILD_PID" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_pid = int(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
if int(payload.get("pid") or 0) == expected_pid:
    path.unlink(missing_ok=True)
PY
  fi

  exit "$status"
}

trap cleanup_pid EXIT INT TERM

.venv/bin/python - "$PID_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pid = int(payload.get("pid") or 0)
except Exception:
    path.unlink(missing_ok=True)
    raise SystemExit(0)
try:
    os.kill(pid, 0)
except ProcessLookupError:
    path.unlink(missing_ok=True)
except PermissionError:
    raise SystemExit(f"Bhiksha pid file points to inaccessible running pid={pid}")
else:
    raise SystemExit(f"Bhiksha is already running with pid={pid}")
PY

.venv/bin/python -m bhiksha.tools.sync_active_plan

COMMAND=(
  "$REPO_ROOT/.venv/bin/python"
  "-u"
  "-m"
  "bhiksha.tools.trade_session"
  --active-plan
  artifacts/playbook/active_plan.json
  --live
)

"${COMMAND[@]}" &
CHILD_PID=$!

.venv/bin/python - "$PID_PATH" "$CHILD_PID" "$LOG_PATH" "$REPO_ROOT" "${COMMAND[@]}" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

pid_path = Path(sys.argv[1])
pid = int(sys.argv[2])
log_path = str(Path(sys.argv[3]).resolve())
repo_root = str(Path(sys.argv[4]).resolve())
command = sys.argv[5:]
metadata = {
    "pid": pid,
    "started_at": datetime.now(UTC).isoformat(),
    "log_path": log_path,
    "active_plan_path": str((Path(repo_root) / "artifacts/playbook/active_plan.json").resolve()),
    "live": True,
    "max_bars": None,
    "command": command,
    "repo_root": repo_root,
    "owner": "cron",
}
pid_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

wait "$CHILD_PID"
