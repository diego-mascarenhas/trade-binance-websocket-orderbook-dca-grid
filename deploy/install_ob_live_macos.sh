#!/usr/bin/env bash
# Install + load LaunchAgents for BTC/ETH/BNB (PAPER).
# Copies runtime files into $HOME so launchd works even if the repo is on
# an external volume (/Volumes/…) blocked by macOS TCC.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHARE="$HOME/.local/share/ob-live"
APP="$SHARE/app"
DEST="$HOME/Library/LaunchAgents"
PY="$(command -v python3)"
SYMBOLS=(BTCUSDT ETHUSDT BNBUSDT)
PORTS=(8765 8766 8767)

mkdir -p "$APP" "$DEST"

echo "syncing runtime → $APP"
for f in \
  ob_live_chart.py \
  orderbook_dca_grid.py \
  ob_ema.py \
  ob_oscillators.py \
  ob_structure.py \
  futures_scan.py \
  telegram_notify.py \
  trade_sounds.py
do
  if [[ -f "$ROOT/$f" ]]; then
    cp -f "$ROOT/$f" "$APP/$f"
  fi
done
if [[ -f "$ROOT/.env" ]]; then
  cp -f "$ROOT/.env" "$APP/.env"
  chmod 600 "$APP/.env"
else
  echo "warning: no .env in repo — LIVE toggle will need keys" >&2
fi

# Local PAPER launcher (no dependency on /Volumes paths at runtime)
cat > "$SHARE/ob_live_start.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
APP="$(cd "$(dirname "$0")/app" && pwd)"
SYMBOL="$(echo "${1:-}" | tr '[:lower:]' '[:upper:]')"
[[ -n "$SYMBOL" ]] || { echo "usage: $0 SYMBOL" >&2; exit 1; }
case "$SYMBOL" in
  BTCUSDT) PORT=8765 ;;
  ETHUSDT) PORT=8766 ;;
  BNBUSDT) PORT=8767 ;;
  *) echo "unsupported symbol: $SYMBOL (fleet is BTC/ETH/BNB)" >&2; exit 1 ;;
esac
PY="${OB_LIVE_PYTHON:-python3}"
cd "$APP"
# Start PAUSED (no Binance REST) — click FEED in UI to resume.
# Slower poll + smaller book when resumed (3 bots / Futures IP weight).
exec "$PY" -u "$APP/ob_live_chart.py" "$SYMBOL" --dry-run --port "$PORT" \
  --feed-paused --sample-sec 1.2 --limit 50
EOF
chmod +x "$SHARE/ob_live_start.sh"

# Remember which python to use
echo "$PY" > "$SHARE/python.path"
# Prefer that python via env in plist... embed absolute path in each plist

for i in "${!SYMBOLS[@]}"; do
  sym="${SYMBOLS[$i]}"
  port="${PORTS[$i]}"
  key="$(echo "$sym" | tr '[:upper:]' '[:lower:]')"
  label="com.oblive.$key"
  dst="$DEST/$label.plist"

  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || \
    launchctl unload "$dst" 2>/dev/null || true

  cat > "$dst" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>WorkingDirectory</key>
  <string>$APP</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OB_LIVE_PYTHON</key>
    <string>$PY</string>
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>$SHARE/ob_live_start.sh</string>
    <string>$sym</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/oblive-$key.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/oblive-$key.err</string>
</dict>
</plist>
EOF

  if launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null; then
    launchctl enable "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null || true
  else
    launchctl load -w "$dst"
  fi
  echo "loaded $label → http://127.0.0.1:$port/  ($sym PAPER)"
done

# Drop symbols no longer in the fleet (e.g. leftover SOL)
for stale in solusdt; do
  label="com.oblive.$stale"
  dst="$DEST/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || \
    launchctl unload "$dst" 2>/dev/null || true
  rm -f "$dst" "$ROOT/deploy/launchagents/$label.plist"
done

# Keep templates in repo for reference
mkdir -p "$ROOT/deploy/launchagents"
cp -f "$DEST"/com.oblive.*.plist "$ROOT/deploy/launchagents/" 2>/dev/null || true

echo
echo "Re-run this script after editing ob_live_chart.py so \$HOME copy stays in sync."
echo "logs: /tmp/oblive-*.log"
