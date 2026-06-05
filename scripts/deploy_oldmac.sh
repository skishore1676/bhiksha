#!/usr/bin/env bash
set -euo pipefail

OLDMAC_HOST="${OLDMAC_HOST:-oldmac}"
REMOTE_DIR="${BHIKSHA_OLDMAC_DIR:-/Users/sunny/Documents/bhiksha}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

