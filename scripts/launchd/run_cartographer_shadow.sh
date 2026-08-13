#!/bin/bash
set -euo pipefail

REPO_ROOT="${1:?Bhiksha repository root is required}"
RECOMMENDATION_ROOT="${2:?Cartographer recommendation root is required}"
DATA_ROOT="${3:?Mala data root is required}"
OUTPUT_ROOT="${4:?Bhiksha observation root is required}"
PYTHON_BIN="${BHIKSHA_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Bhiksha Python is unavailable: $PYTHON_BIN" >&2
  exit 2
fi

cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m bhiksha.tools.cartographer_shadow observe-root \
  --recommendation-root "$RECOMMENDATION_ROOT" \
  --mala-data-root "$DATA_ROOT" \
  --output-root "$OUTPUT_ROOT"
