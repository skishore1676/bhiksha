#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CARTOGRAPHER_ROOT="${CARTOGRAPHER_REPO_ROOT:-/Users/sunny/Documents/market-cartographer}"
RECOMMENDATION_ROOT="${CARTOGRAPHER_ALPHA_OUTPUT_ROOT:-$CARTOGRAPHER_ROOT/artifacts/alpha-lab}"
DATA_ROOT="${CARTOGRAPHER_MALA_DATA_ROOT:-/Users/sunny/Documents/mala_v2/data}"
OUTPUT_ROOT="${BHIKSHA_CARTOGRAPHER_OUTPUT_ROOT:-$REPO_ROOT/artifacts/cartographer-shadow}"
LOG_ROOT="${BHIKSHA_CARTOGRAPHER_LOG_ROOT:-$OUTPUT_ROOT/logs}"
PLIST="$HOME/Library/LaunchAgents/com.bhiksha.cartographer-shadow.plist"
TEMPLATE="$REPO_ROOT/scripts/launchd/com.bhiksha.cartographer-shadow.plist.template"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$OUTPUT_ROOT" "$LOG_ROOT"
sed \
  -e "s#__REPO_ROOT__#$REPO_ROOT#g" \
  -e "s#__RECOMMENDATION_ROOT__#$RECOMMENDATION_ROOT#g" \
  -e "s#__DATA_ROOT__#$DATA_ROOT#g" \
  -e "s#__OUTPUT_ROOT__#$OUTPUT_ROOT#g" \
  -e "s#__LOG_ROOT__#$LOG_ROOT#g" \
  "$TEMPLATE" > "$PLIST"
plutil -lint "$PLIST"
launchctl bootout "$DOMAIN/com.bhiksha.cartographer-shadow" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl print "$DOMAIN/com.bhiksha.cartographer-shadow"
