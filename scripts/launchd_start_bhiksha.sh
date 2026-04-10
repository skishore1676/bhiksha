#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$REPO_ROOT"
export PYTHONPATH=src

exec "$REPO_ROOT/.venv/bin/python" -m bhiksha.tools.server_session restart --live
