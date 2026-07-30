#!/usr/bin/env bash
set -euo pipefail
DEST="$HOME/Library/LaunchAgents"
SHARE="$HOME/.local/share/ob-live"
# Active fleet + stale labels to always clean up
SYMBOLS=(btcusdt ethusdt bnbusdt solusdt)
for s in "${SYMBOLS[@]}"; do
  label="com.oblive.$s"
  dst="$DEST/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || \
    launchctl unload "$dst" 2>/dev/null || true
  rm -f "$dst"
  echo "removed $label"
done
# leave $SHARE/app in place unless --purge
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$SHARE"
  echo "purged $SHARE"
fi
