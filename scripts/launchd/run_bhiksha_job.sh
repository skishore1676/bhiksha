#!/usr/bin/env bash
set -euo pipefail

RUNNER_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

ulimit -n 10240 || true
export PYTHONPATH="$REPO_ROOT/src${BHIKSHA_KERNEL_SRC:+:$BHIKSHA_KERNEL_SRC}"
export PYTHONUNBUFFERED=1

python_bin="${BHIKSHA_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  python_bin="python3"
fi

# Installer-owned, non-secret runtime marker. This exact allowlisted flag is
# read by every entrypoint using the runner (scheduled start, watchdog restart,
# Lathi ensure-live-runtime, and manual recovery), so crash recovery cannot
# silently change the observational mode.
exit_edge_marker="$REPO_ROOT/artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled"
if [ -f "$exit_edge_marker" ]; then
  export BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=true
else
  export BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=false
fi

set +e
"$python_bin" -m bhiksha.tools.launchd_job "$@"
rc=$?
if [ $rc -ne 0 ]; then
  job_name="${1:-unknown}"
  "$python_bin" - "$REPO_ROOT" "$job_name" "$rc" <<'PY' 2>/dev/null || true
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

repo_root = Path(sys.argv[1])
job = sys.argv[2]
rc = sys.argv[3]
path = repo_root / "artifacts" / "playbook" / "launchd" / "latest_status.json"
path.parent.mkdir(parents=True, exist_ok=True)
try:
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
except Exception:
    data = {}
if not isinstance(data.get("jobs"), dict):
    data["jobs"] = {}
existing = data["jobs"].get(job) if isinstance(data["jobs"].get(job), dict) else None
existing_at = existing.get("recorded_at") if isinstance(existing, dict) else None
should_write = True
if existing_at:
    try:
        from datetime import datetime as _dt
        prev = _dt.fromisoformat(str(existing_at).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if (now - prev).total_seconds() < 5:
            should_write = False
    except Exception:
        pass
if should_write:
    data["jobs"][job] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "label": f"com.bhiksha.{job}",
        "payload": {
            "job": job,
            "status": "failed",
            "return_code": int(rc),
            "reason": "runner_crash_before_status_write",
            "stderr_tail": f"run_bhiksha_job.sh captured non-zero exit {rc} without a payload — likely import-time failure",
        },
    }
    data["generated_at"] = data["jobs"][job]["recorded_at"]
    data["schema"] = "bhiksha.launchd.latest_status.v1"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
PY
fi
exit $rc
