#!/bin/bash
set -euo pipefail

REPO_ROOT="/Users/suman/kg_env/projects/bhiksha"

cd "$REPO_ROOT"
export PYTHONPATH=src

exec "$REPO_ROOT/.venv/bin/python" -m bhiksha.tools.server_session stop
