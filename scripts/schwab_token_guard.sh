#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin=".venv/bin/python"
if [ ! -x "$python_bin" ]; then
  python_bin="python3"
fi

mode="${1:-premarket}"
shift || true

PYTHONPATH=src "$python_bin" -m bhiksha.tools.schwab_token_guard \
  --mode "$mode" \
  "$@"
