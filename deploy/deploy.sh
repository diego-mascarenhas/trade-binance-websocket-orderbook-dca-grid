#!/usr/bin/env bash
# Production code update for the VPS (systemd fleet).
#
# Typical use on the server:
#   cd /opt/trade-binance-websocket-orderbook-dca-grid
#   ./deploy/deploy.sh
#
# Options:
#   --no-pull     skip git pull (restart only)
#   --branch X    checkout/pull this branch (default: current, or DEPLOY_BRANCH)
#   --dry-run     show what would run
#   --skip-api    do not restart dca-api
#   --skip-tg     do not restart dca-telegram-ctl
#   --status      after update, print sync_pairs status
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${DEPLOY_BRANCH:-}"
DO_PULL=1
DRY_RUN=0
RESTART_API=1
RESTART_TG=1
SHOW_STATUS=0

usage() {
  cat <<'EOF'
Production code update for the VPS (systemd fleet).

  ./deploy/deploy.sh
  ./deploy/deploy.sh --status
  ./deploy/deploy.sh --no-pull          # restart only (.env / local edits)
  ./deploy/deploy.sh --branch main
  ./deploy/deploy.sh --dry-run
  ./deploy/deploy.sh --skip-api --skip-tg

Env: DEPLOY_BRANCH=main ./deploy/deploy.sh
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) DO_PULL=0; shift ;;
    --branch) BRANCH="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-api) RESTART_API=0; shift ;;
    --skip-tg) RESTART_TG=0; shift ;;
    --status) SHOW_STATUS=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; echo
  else
    echo "+ $*"
    "$@"
  fi
}

echo "==> Deploy root: $ROOT"
if [[ -n "$BRANCH" ]]; then
  run git fetch --prune origin
  run git checkout "$BRANCH"
fi

if [[ "$DO_PULL" -eq 1 ]]; then
  run git pull --ff-only
else
  echo "==> Skipping git pull (--no-pull)"
fi

# Reload unit templates if present (safe when unchanged).
if [[ -d /etc/systemd/system ]]; then
  UNIT_SRC=()
  for u in dca-futures@.service dca-spot@.service dca-telegram-ctl.service dca-api.service pump-stall-watch.service; do
    [[ -f "deploy/$u" ]] && UNIT_SRC+=("deploy/$u")
  done
  if [[ ${#UNIT_SRC[@]} -gt 0 ]]; then
    run sudo cp "${UNIT_SRC[@]}" /etc/systemd/system/
    run sudo systemctl daemon-reload
  fi
fi

# Restart trading fleet so processes load new code + .env (EXIT_MODE, STRUCTURE_INTERVAL, …).
if [[ -f deploy/sync_pairs.py ]]; then
  run python3 deploy/sync_pairs.py --restart
else
  echo "WARN: deploy/sync_pairs.py missing — restart units manually" >&2
fi

if [[ "$RESTART_TG" -eq 1 ]]; then
  if systemctl list-unit-files dca-telegram-ctl.service &>/dev/null; then
    run sudo systemctl restart dca-telegram-ctl
  else
    echo "==> dca-telegram-ctl not installed — skip"
  fi
fi

if [[ "$RESTART_API" -eq 1 ]]; then
  if systemctl list-unit-files dca-api.service &>/dev/null; then
    run sudo systemctl restart dca-api
  else
    echo "==> dca-api not installed — skip"
  fi
fi

# Restart pump-stall orchestrator only if already enabled (does not auto-enable).
if systemctl is-enabled pump-stall-watch.service &>/dev/null; then
  run sudo systemctl restart pump-stall-watch
else
  echo "==> pump-stall-watch not enabled — skip (install: systemctl enable --now pump-stall-watch)"
fi

if [[ "$SHOW_STATUS" -eq 1 ]]; then
  run python3 deploy/sync_pairs.py status
  run sudo systemctl --no-pager --full status dca-api dca-telegram-ctl pump-stall-watch || true
fi

echo "==> Deploy done."
echo "    Structure TP: set EXIT_MODE=structure (and optional STRUCTURE_INTERVAL=15m) in .env, then re-run."
echo "    One symbol:   sudo systemctl restart 'dca-futures@SOLUSDT'"
echo "    Pump-stall:   sudo systemctl status pump-stall-watch"
echo "                  sudo journalctl -u pump-stall-watch -f"
