#!/bin/bash
set -euo pipefail

REPO_ROOT="/Users/sunny/Documents/bhiksha"
cd "$REPO_ROOT"
ulimit -n 10240
export PYTHONPATH=src
export PYTHONUNBUFFERED=1

.venv/bin/python -m bhiksha.tools.sync_active_plan

exec .venv/bin/python -u -m bhiksha.tools.trade_session \
  --active-plan artifacts/playbook/active_plan.json \
  --live
