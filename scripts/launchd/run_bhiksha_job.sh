#!/usr/bin/env bash
set -euo pipefail

RUNNER_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

ulimit -n 10240 || true
export PYTHONPATH="$REPO_ROOT/src${BHIKSHA_KERNEL_SRC:+:$BHIKSHA_KERNEL_SRC}"
export PYTHONUNBUFFERED=1

python_bin="${BHIKSHA_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ "${1:-}" = "chart-scenario-shadow" ]; then
  export PYTHONDONTWRITEBYTECODE=1
  case "$python_bin" in
    /*) ;;
    *) echo "chart-scenario-shadow requires an absolute BHIKSHA_PYTHON" >&2; exit 2 ;;
  esac
  if [ ! -x "$python_bin" ]; then
    echo "chart-scenario-shadow requires an executable BHIKSHA_PYTHON" >&2
    exit 2
  fi
  : "${BHIKSHA_CHART_PYTHON_REALPATH:?missing pinned chart Python realpath}"
  : "${BHIKSHA_CHART_PYTHON_SHA256:?missing pinned chart Python digest}"
  : "${BHIKSHA_CHART_PYTHON_VERSION:?missing pinned chart Python version}"
  : "${BHIKSHA_CHART_RUNNER_SHA256:?missing pinned chart runner digest}"
  : "${BHIKSHA_CHART_REPO_COMMIT:?missing pinned chart repository commit}"
  if [ ! "$python_bin" -ef "$BHIKSHA_CHART_PYTHON_REALPATH" ]; then
    echo "chart-scenario-shadow Python realpath drift" >&2
    exit 2
  fi
  python_hash="$(/usr/bin/shasum -a 256 "$python_bin" | /usr/bin/awk '{print $1}')"
  runner_hash="$(/usr/bin/shasum -a 256 "$RUNNER_PATH" | /usr/bin/awk '{print $1}')"
  python_version="$($python_bin --version 2>&1)"
  repo_commit="$(/usr/bin/git -C "$REPO_ROOT" rev-parse HEAD)"
  repo_status="$(/usr/bin/git -C "$REPO_ROOT" status --porcelain)"
  if [ "$python_hash" != "$BHIKSHA_CHART_PYTHON_SHA256" ] || \
     [ "$runner_hash" != "$BHIKSHA_CHART_RUNNER_SHA256" ] || \
     [ "$python_version" != "$BHIKSHA_CHART_PYTHON_VERSION" ] || \
     [ "$repo_commit" != "$BHIKSHA_CHART_REPO_COMMIT" ] || \
     [ -n "$repo_status" ]; then
    echo "chart-scenario-shadow frozen coordinator runtime drift" >&2
    exit 2
  fi
elif [ ! -x "$python_bin" ]; then
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

export BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=false
if [ "${1:-}" = "chart-scenario-shadow" ]; then
  chart_env=(
    "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    "PYTHONPATH=$REPO_ROOT/src:$BHIKSHA_KERNEL_SRC"
    "PYTHONUNBUFFERED=1"
    "PYTHONDONTWRITEBYTECODE=1"
    "BHIKSHA_SANITIZED_SUBPROCESS=1"
  )
  chart_allowed=(
    BHIKSHA_KERNEL_SRC
    BHIKSHA_CHART_KERNEL_RUNTIME_RECORD
    BHIKSHA_CHART_KERNEL_RUNTIME_HASH
    BHIKSHA_PYTHON
    BHIKSHA_CHART_PYTHON_REALPATH
    BHIKSHA_CHART_PYTHON_SHA256
    BHIKSHA_CHART_PYTHON_VERSION
    BHIKSHA_CHART_RUNNER_SHA256
    BHIKSHA_CHART_REPO_COMMIT
    BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT
    BHIKSHA_CHART_SCENARIO_CAMPAIGN_CONFIG
    BHIKSHA_CHART_SCENARIO_DAILY_CONTRACT_DIR
    BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH
    SCHWAB_TOKEN_FILE
    SCHWAB_API_BASE_URL
    SCHWAB_TIMEOUT_SECONDS
    SSL_CERT_FILE
    SSL_CERT_DIR
    REQUESTS_CA_BUNDLE
    TMPDIR
    LANG
    LC_ALL
    BHIKSHA_CHART_SCENARIO_COMMAND_TIMEOUT_SECONDS
    BHIKSHA_LAUNCHD_JOB_TIMEOUT_SECONDS
  )
  for name in "${chart_allowed[@]}"; do
    if [ "${!name+x}" = x ]; then
      chart_env+=("$name=${!name}")
    fi
  done
  /usr/bin/env -i "${chart_env[@]}" \
    "$python_bin" -m bhiksha.tools.chart_kernel_runtime verify
  chart_root="${BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT:-$REPO_ROOT/artifacts/chart_scenarios}"
  chart_scenario_marker="$(/usr/bin/env -i "${chart_env[@]}" "$python_bin" - "$chart_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
if not root.is_absolute():
    raise SystemExit("chart artifact root must be absolute")
for candidate in (root, *root.parents):
    if candidate.is_symlink():
        raise SystemExit(f"chart artifact root cannot contain a symlink: {candidate}")
resolved = root.resolve()
if resolved.parts[-2:] != ("artifacts", "chart_scenarios"):
    raise SystemExit("chart artifact root must end in artifacts/chart_scenarios")
marker = resolved / "launchd" / "chart_scenario_shadow.enabled"
if marker.is_symlink() or any(parent.is_symlink() for parent in marker.parents):
    raise SystemExit("chart runtime marker cannot contain a symlink")
print(marker)
PY
)"
  if [ -f "$chart_scenario_marker" ]; then
    chart_env+=("BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=true")
  else
    chart_env+=("BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=false")
  fi
  set +e
  /usr/bin/env -i "${chart_env[@]}" \
    "$python_bin" -m bhiksha.tools.launchd_job "$@"
  rc=$?
  if [ $rc -ne 0 ]; then
    # Best-effort breadcrumb when the job crashes before it can write
    # latest_status itself (e.g. missing dep after a deploy). Use a
    # minimal payload that does not import bhiksha.
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
data["jobs"][job] = {
    "recorded_at": datetime.now(UTC).isoformat(),
    "label": f"com.bhiksha.{job}",
    "payload": {
        "job": job,
        "status": "failed",
        "return_code": int(rc),
        "reason": "runner_crash_before_status_write",
        "stderr_tail": f"run_bhiksha_job.sh captured non-zero exit {rc} without a payload — likely import-time failure (missing dep or kernel env)",
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
