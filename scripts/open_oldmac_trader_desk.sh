#!/usr/bin/env bash
set -euo pipefail

OLDMAC_HOST="${OLDMAC_HOST:-oldmac}"
REMOTE_REPO="${REMOTE_REPO:-/Users/sunny/Documents/bhiksha}"
REMOTE_MALA_REPO="${REMOTE_MALA_REPO:-../mala_v2}"
LOCAL_PORT="${TRADER_DESK_LOCAL_PORT:-8766}"
REMOTE_PORT="${TRADER_DESK_REMOTE_PORT:-8766}"
PID_PATH="/tmp/bhiksha_trader_desk_${REMOTE_PORT}.pid"
LOG_PATH="/tmp/bhiksha_trader_desk_${REMOTE_PORT}.log"

ssh "$OLDMAC_HOST" "cd '$REMOTE_REPO' && if ! lsof -nP -iTCP:$REMOTE_PORT -sTCP:LISTEN >/dev/null 2>&1; then MALA_REPO_ROOT='$REMOTE_MALA_REPO' PYTHONPATH=src nohup ./.venv/bin/python -m bhiksha.tools.trader_desk --host 127.0.0.1 --port '$REMOTE_PORT' > '$LOG_PATH' 2>&1 & echo \$! > '$PID_PATH'; fi"

if ! lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  ssh -N -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "$OLDMAC_HOST" &
  echo $! > "/tmp/bhiksha_trader_desk_tunnel_${LOCAL_PORT}.pid"
  sleep 1
fi

URL="http://127.0.0.1:${LOCAL_PORT}"
if command -v open >/dev/null 2>&1; then
  open "$URL"
else
  echo "$URL"
fi
