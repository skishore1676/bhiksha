#!/bin/bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
CARTOGRAPHER_ROOT="${CARTOGRAPHER_REPO_ROOT:-/Users/sunny/Documents/market-cartographer}"
RECOMMENDATION_ROOT="${CARTOGRAPHER_ALPHA_OUTPUT_ROOT:-$CARTOGRAPHER_ROOT/artifacts/alpha-lab}"
DATA_ROOT="${CARTOGRAPHER_MALA_DATA_ROOT:-/Users/sunny/Documents/mala_v2/research/results/cache_recovery/market_cartographer/mcse-2026w33-v2}"
OUTPUT_ROOT="${BHIKSHA_CARTOGRAPHER_OUTPUT_ROOT:-$REPO_ROOT/artifacts/cartographer-shadow}"
LAUNCHD_DIR="${BHIKSHA_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$LAUNCHD_DIR/com.bhiksha.cartographer-shadow.plist"
DOMAIN="gui/$(id -u)"
: "${BHIKSHA_CARTOGRAPHER_SHEET_ID:?set the approved target Sheet id before installation}"
: "${BHIKSHA_CARTOGRAPHER_SHEET_CREDENTIALS:?set the approved credential file path before installation}"

mkdir -p "$LAUNCHD_DIR" "$OUTPUT_ROOT" "$REPO_ROOT/artifacts/playbook/launchd"
PYTHONPATH="$REPO_ROOT/src" "${BHIKSHA_PYTHON:-$REPO_ROOT/.venv/bin/python}" - \
  "$REPO_ROOT" "$PLIST" "$CARTOGRAPHER_ROOT" \
  "$RECOMMENDATION_ROOT" "$DATA_ROOT" "$OUTPUT_ROOT" \
  "$BHIKSHA_CARTOGRAPHER_SHEET_ID" "$BHIKSHA_CARTOGRAPHER_SHEET_CREDENTIALS" <<'PY'
import os
import plistlib
import sys
import tempfile
from pathlib import Path

from bhiksha.ops.launchd_registry import job_by_runner

repo = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2]).expanduser()
job = job_by_runner("cartographer-shadow")
if job is None:
    raise SystemExit("cartographer-shadow is missing from the launchd registry")
payload = job.plist_payload(
    repo_root=repo,
    environment={
        "CARTOGRAPHER_REPO_ROOT": sys.argv[3],
        "CARTOGRAPHER_ALPHA_OUTPUT_ROOT": sys.argv[4],
        "CARTOGRAPHER_MALA_DATA_ROOT": sys.argv[5],
        "BHIKSHA_CARTOGRAPHER_OUTPUT_ROOT": sys.argv[6],
        "BHIKSHA_CARTOGRAPHER_SHEET_ID": sys.argv[7],
        "BHIKSHA_CARTOGRAPHER_SHEET_CREDENTIALS": sys.argv[8],
    },
)
path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(plistlib.dumps(payload, sort_keys=True))
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
PY
plutil -lint "$PLIST"
launchctl bootout "$DOMAIN/com.bhiksha.cartographer-shadow" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl print "$DOMAIN/com.bhiksha.cartographer-shadow" >/dev/null
echo "LOADED com.bhiksha.cartographer-shadow"
