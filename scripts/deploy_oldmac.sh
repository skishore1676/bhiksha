#!/usr/bin/env bash
set -euo pipefail

OLDMAC_HOST="${OLDMAC_HOST:-oldmac}"
REMOTE_DIR="${BHIKSHA_OLDMAC_DIR:-/Users/sunny/Documents/bhiksha}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Refuse to ship uncommitted code: a dirty rsync deploy leaves no record of
# what actually ran on the trading host. Set BHIKSHA_DEPLOY_ALLOW_DIRTY=1 to
# override deliberately.
if [ "${BHIKSHA_DEPLOY_ALLOW_DIRTY:-0}" != "1" ]; then
  if [ -n "$(git -C "${ROOT_DIR}" status --porcelain 2>/dev/null)" ]; then
    echo "deploy_oldmac: source tree is dirty; commit first or set BHIKSHA_DEPLOY_ALLOW_DIRTY=1" >&2
    exit 1
  fi
fi
echo "deploy_oldmac: deploying commit $(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"

rsync -az \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".env" \
  --exclude "__pycache__" \
  --exclude ".pytest_cache" \
  --exclude "artifacts" \
  --exclude "bhiksha.db" \
  --exclude "config/schwab_tokens.json" \
  --exclude "config/public_session.json" \
  "${ROOT_DIR}/" "${OLDMAC_HOST}:${REMOTE_DIR}/"

