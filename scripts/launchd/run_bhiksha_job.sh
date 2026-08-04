#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

ulimit -n 10240 || true
export PYTHONPATH="$REPO_ROOT/src${BHIKSHA_KERNEL_SRC:+:$BHIKSHA_KERNEL_SRC}"
export PYTHONUNBUFFERED=1

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

chart_scenario_marker="$REPO_ROOT/artifacts/playbook/runtime_flags/chart_scenario_shadow.enabled"
if [ -f "$chart_scenario_marker" ]; then
  export BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=true
else
  export BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=false
fi

python_bin="${BHIKSHA_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  if [ "${1:-}" = "chart-scenario-shadow" ]; then
    echo "chart-scenario-shadow requires an absolute executable BHIKSHA_PYTHON or repo .venv" >&2
    exit 2
  fi
  python_bin="python3"
fi

exec "$python_bin" -m bhiksha.tools.launchd_job "$@"
