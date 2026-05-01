#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DAYS="${BHIKSHA_CHART_REVIEW_DAYS:-7}"
PORT="${BHIKSHA_CHART_REVIEW_PORT:-8765}"
REMOTE_DB="${BHIKSHA_CHART_REVIEW_REMOTE_DB:-oldmac:/Users/sunny/Documents/bhiksha/bhiksha.db}"
OUTPUT_DIR="${BHIKSHA_CHART_REVIEW_OUTPUT_DIR:-artifacts/chart_review}"

ARGS=(
  --remote-db "$REMOTE_DB"
  --output-dir "$OUTPUT_DIR"
  --days "$DAYS"
  --closed-only
)

if [[ "${BHIKSHA_CHART_REVIEW_ALL:-0}" == "1" ]]; then
  ARGS=(
    --remote-db "$REMOTE_DB"
    --output-dir "$OUTPUT_DIR"
    --all
    --closed-only
  )
fi

if [[ "${BHIKSHA_CHART_REVIEW_EXTENDED_HOURS:-0}" == "1" ]]; then
  ARGS+=(--include-extended-hours)
fi

PYTHONPATH=src .venv/bin/python -m bhiksha.tools.chart_review "${ARGS[@]}"

cat <<EOF

Open:
  http://127.0.0.1:${PORT}

Serve if needed:
  .venv/bin/python -m http.server ${PORT} --directory ${OUTPUT_DIR}
EOF

if [[ "${BHIKSHA_CHART_REVIEW_SERVE:-0}" == "1" ]]; then
  exec .venv/bin/python -m http.server "$PORT" --directory "$OUTPUT_DIR"
fi
