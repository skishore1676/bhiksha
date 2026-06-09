#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/schwab_refresh.sh url
  scripts/schwab_refresh.sh exchange '<returned_callback_url>'
  scripts/schwab_refresh.sh check

Run from the Bhiksha repo on the machine whose Schwab token file should be
updated. On oldmac that is usually:

  cd /Users/sunny/Documents/bhiksha
  scripts/schwab_refresh.sh url
  scripts/schwab_refresh.sh exchange 'https://127.0.0.1:8080/?code=...'
  scripts/schwab_refresh.sh check
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin=".venv/bin/python"
if [ ! -x "$python_bin" ]; then
  python_bin="python3"
fi

command="${1:-}"

case "$command" in
  url)
    PYTHONPATH=src "$python_bin" -m bhiksha.tools.schwab_auth url
    ;;
  exchange)
    callback_url="${2:-}"
    if [ -z "$callback_url" ]; then
      usage
      exit 1
    fi
    PYTHONPATH=src "$python_bin" -m bhiksha.tools.schwab_auth exchange "$callback_url"
    ;;
  check)
    PYTHONPATH=src "$python_bin" - <<'PY'
from datetime import UTC, datetime

from bhiksha.integrations.schwab.auth import access_token_is_stale, refresh_token_is_expired
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens

settings = SchwabSettings.from_env()
payload = read_tokens(settings.token_file)
if not payload:
    raise SystemExit(f"NO_TOKEN_FILE {settings.token_file}")

refresh_issued = payload.get("refresh_token_issued")
access_issued = payload.get("access_token_issued")
refresh_expired = refresh_token_is_expired(payload)
access_stale = access_token_is_stale(payload)

print(f"TOKEN_FILE={settings.token_file}")
print(f"ACCESS_TOKEN_ISSUED={access_issued}")
print(f"REFRESH_TOKEN_ISSUED={refresh_issued}")
print(f"ACCESS_TOKEN_STALE={access_stale}")
print(f"REFRESH_TOKEN_EXPIRED={refresh_expired}")
print(f"CHECKED_AT={datetime.now(UTC).isoformat()}")

if refresh_expired:
    raise SystemExit(2)
PY
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    printf "Unknown command: %s\n\n" "$command" >&2
    usage
    exit 1
    ;;
esac
