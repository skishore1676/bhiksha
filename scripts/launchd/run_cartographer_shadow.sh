#!/bin/bash
set -euo pipefail

REPO_ROOT="${1:?Bhiksha repository root is required}"
RECOMMENDATION_ROOT="${2:?Cartographer recommendation root is required}"
DATA_ROOT="${3:?Mala data root is required}"
OUTPUT_ROOT="${4:?Bhiksha observation root is required}"
PYTHON_BIN="${BHIKSHA_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
CARTOGRAPHER_REPO_ROOT="${CARTOGRAPHER_REPO_ROOT:?Cartographer repository root is required}"
CARTOGRAPHER_PYTHON_BIN="${CARTOGRAPHER_PYTHON_BIN:-$CARTOGRAPHER_REPO_ROOT/.venv/bin/python}"
PROJECTION_RECEIPT="${BHIKSHA_CARTOGRAPHER_PROJECTION_RECEIPT:-$OUTPUT_ROOT/projection/latest.json}"

if [[ ! -x "$PYTHON_BIN" || ! -x "$CARTOGRAPHER_PYTHON_BIN" ]]; then
  echo "Bhiksha Python is unavailable: $PYTHON_BIN" >&2
  exit 2
fi

# The producer's terminal receipt is the single input authority. A stale or
# unknown owner never becomes a successful projection merely because the
# diagnostic price observer still has data.
producer_status="$($CARTOGRAPHER_PYTHON_BIN -m market_cartographer.alpha_cli status \
  --output-root "$RECOMMENDATION_ROOT")"
mkdir -p "$(dirname "$PROJECTION_RECEIPT")"
printf '%s\n' "$producer_status" > "$(dirname "$PROJECTION_RECEIPT")/producer-status.json"
if ! "$PYTHON_BIN" - "$producer_status" <<'PY'
import json
import sys
status = json.loads(sys.argv[1])
raise SystemExit(0 if status.get("lifecycle") == "complete" else 1)
PY
then
  echo "CARTOGRAPHER_SHADOW_BLOCKED reason=producer_terminal_evidence_not_fresh" >&2
  exit 3
fi

: "${BHIKSHA_CARTOGRAPHER_SHEET_ID:?Cartographer Sheet id is required}"
: "${BHIKSHA_CARTOGRAPHER_SHEET_CREDENTIALS:?Cartographer Sheet credentials path is required}"
: "${BHIKSHA_CARTOGRAPHER_PREMIUM_CEILING:?Cartographer premium ceiling is required}"

cd "$REPO_ROOT"
"$PYTHON_BIN" -m bhiksha.tools.cartographer_projector \
  --signal-batch "$RECOMMENDATION_ROOT/latest/signals.json" \
  --trading-date "$(date +%F)" \
  --premium-ceiling "$BHIKSHA_CARTOGRAPHER_PREMIUM_CEILING" \
  --spreadsheet-id "$BHIKSHA_CARTOGRAPHER_SHEET_ID" \
  --credentials "$BHIKSHA_CARTOGRAPHER_SHEET_CREDENTIALS" \
  --apply \
  --receipt "$PROJECTION_RECEIPT"

# Retained as a diagnostic observer only. It is deliberately after projection,
# so a projection failure cannot be hidden by a healthy price observation.
if ! "$PYTHON_BIN" -m bhiksha.tools.cartographer_shadow observe-root \
  --recommendation-root "$RECOMMENDATION_ROOT" \
  --mala-data-root "$DATA_ROOT" \
  --output-root "$OUTPUT_ROOT"; then
  echo "CARTOGRAPHER_SHADOW_DIAGNOSTIC_DEGRADED reason=price_observer_failed" >&2
fi
