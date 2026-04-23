#!/bin/bash
set -euo pipefail

REPO_ROOT="/Users/sunny/Documents/bhiksha"
cd "$REPO_ROOT"
ulimit -n 10240
export PYTHONPATH=src
export PYTHONUNBUFFERED=1

"$REPO_ROOT/.venv/bin/python" -m bhiksha.tools.server_session ensure-running --live
