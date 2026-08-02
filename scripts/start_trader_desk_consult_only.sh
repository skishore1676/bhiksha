#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MALA_ROOT="${MALA_REPO_ROOT:-$REPO_ROOT/../mala_v2}"
KERNEL_ROOT="${MALA_BHIKSHA_KERNEL_ROOT:-$REPO_ROOT/../mala-bhiksha-kernel}"
PYTHON_BIN="${BHIKSHA_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
PORT="${BHIKSHA_CONSULT_PORT:-8767}"
PACKET="$MALA_ROOT/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v1.json"
CAPABILITIES="$REPO_ROOT/artifacts/capabilities/bhiksha_packet_capabilities_v1.json"
LEGACY_REPORT="$REPO_ROOT/artifacts/legacy_retirement/current.json"

cd "$REPO_ROOT"
export MALA_REPO_ROOT="$MALA_ROOT"
export PYTHONPATH="$KERNEL_ROOT/src:$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m bhiksha.tools.trader_desk_consult \
  --host 127.0.0.1 \
  --port "$PORT" \
  --packet "$PACKET" \
  --mala-repo "$MALA_ROOT" \
  --capability-manifest "$CAPABILITIES" \
  --legacy-retirement-report "$LEGACY_REPORT" \
  --artifact-root "$REPO_ROOT/artifacts/playbook"
