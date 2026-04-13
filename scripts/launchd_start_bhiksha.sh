#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$REPO_ROOT"
ulimit -n 10240 || true
export PYTHONPATH=src
export PYTHONUNBUFFERED=1

exec "$REPO_ROOT/.venv/bin/python" -m bhiksha.tools.server_session restart --live
