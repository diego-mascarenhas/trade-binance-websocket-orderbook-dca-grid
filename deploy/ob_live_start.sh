#!/usr/bin/env bash
# Start ob_live_chart.py in PAPER (--dry-run) with a fixed port per symbol.
set -euo pipefail

if [[ -n "${OB_LIVE_ROOT:-}" ]]; then
  ROOT="$OB_LIVE_ROOT"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
SYMBOL="$(echo "${1:-}" | tr '[:lower:]' '[:upper:]')"
if [[ -z "$SYMBOL" ]]; then
  echo "usage: $0 SYMBOL" >&2
  exit 1
fi

case "$SYMBOL" in
  BTCUSDT) PORT=8765 ;;
  ETHUSDT) PORT=8766 ;;
  BNBUSDT) PORT=8767 ;;
  SOLUSDT) PORT=8768 ;;
  *)
    # Unknown symbols: derive a stable port in 8770–8799
    HASH=$(printf '%s' "$SYMBOL" | cksum | awk '{print $1}')
    PORT=$((8770 + HASH % 30))
    ;;
esac

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

cd "$ROOT"
exec "$PY" -u "$ROOT/ob_live_chart.py" "$SYMBOL" --dry-run --port "$PORT"
