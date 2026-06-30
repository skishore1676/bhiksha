#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${BHIKSHA_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

ulimit -n 10240 || true
export PYTHONPATH=src
export PYTHONUNBUFFERED=1

python_bin="${BHIKSHA_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  python_bin="python3"
fi

exec "$python_bin" -m bhiksha.tools.launchd_job "$@"
