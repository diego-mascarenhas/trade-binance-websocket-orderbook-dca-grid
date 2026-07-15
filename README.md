# trade-binance-websocket-orderbook-dca-grid

Order-book anchored **DCA grid** bots for **Binance Futures USDT-M** and **Binance Spot**.

Repository: [github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid](https://github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid)

Both scripts read the live order book, place DCA orders on real walls, size entries from wallet %, and manage exits anchored to the book:

| Bot | Market | Entry | Exit |
|-----|--------|-------|------|
| `orderbook_dca_grid.py` | Futures | LONG/SHORT grid on walls | **Staged** (default): TP1 + SL@entry + trail |
| `orderbook_dca_grid_spot.py` | Spot | BUY grid on bid walls | OCO (TP + SL) |

`orderbook_staged_exit.py` is legacy/experimental — use **`orderbook_dca_grid.py --supervise`** with staged exit (built-in) instead of two processes.

Self-contained: **Python standard library only** — no third-party dependencies.

## Project layout

```
trade-binance-websocket-orderbook-dca-grid/
├── orderbook_dca_grid.py        # Futures bot (grid + exit plugins)
├── orderbook_staged_exit.py     # Staged exit logic (used by exits/staged.py; standalone optional)
├── orderbook_dca_grid_spot.py   # Spot bot
├── botctl.py                    # CLI: start/stop/status per symbol
├── telegram_botctl.py           # Telegram remote control daemon
├── telegram_notify.py           # Telegram alerts (send-only)
├── exits/                       # Exit plugins (staged, trailing)
├── .state/                      # Staged exit state per symbol (gitignored)
├── .run/                        # PID files + logs (Mac pidfile backend; gitignored)
├── pyproject.toml
├── .env.example
└── deploy/
    ├── dca-futures@.service       # Futures supervisor (--supervise)
    ├── dca-telegram-ctl.service   # Telegram start/stop/status daemon
    ├── dca-futures-tp@.service    # Futures: trailing TP only (--tp-only)
    ├── dca-staged-exit@.service   # Legacy — do not use with dca-futures@
    ├── dca-spot@.service
    └── sync_pairs.py              # Start/stop fleet from FUTURES_PAIRS / SPOT_PAIRS
```

## Quick start

```bash
git clone https://github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid.git
cd trade-binance-websocket-orderbook-dca-grid
cp .env.example .env            # fill in BINANCE_API_KEY / BINANCE_SECRET_KEY
chmod 600 .env
```

Keys are read from environment variables first, then `.env` (cwd or next to the script). No `pip install` is required to run.

Preview without sending orders:

```bash
python3 orderbook_dca_grid.py ADAUSDT --dry-run
python3 orderbook_dca_grid_spot.py BTCUSDT --dry-run
```

---

## Futures (`orderbook_dca_grid.py`)

Runs on **Binance USDT-M Futures** (`fapi.binance.com`).

### Modes

```bash
# Place grid once + auto-manage trailing TP (foreground loop) — default
python3 orderbook_dca_grid.py ADAUSDT

# Only manage trailing TP for an existing position (no new grid)
python3 orderbook_dca_grid.py ADAUSDT --tp-only

# Fully autonomous (recommended — defaults: staged exit, auto direction)
python3 orderbook_dca_grid.py ADAUSDT --supervise

# Cancel + fresh grid (DCA-only if holding; stop systemd unit first)
python3 orderbook_dca_grid.py OPUSDT --rearm --dry-run
python3 orderbook_dca_grid.py OPUSDT --rearm
python3 orderbook_dca_grid.py OPUSDT --rearm --rearm-flat  # close position, then full grid
```

### Key behavior

- **Direction**: `--direction auto` (default) from bid/ask imbalance; or `long` / `short`.
- **Account risk guards** (futures, before arming a grid):
  - `MAX_IMBALANCE=20` (default): skip new grids on the heavier LONG/SHORT side when imbalance exceeds 20%.
  - `MAX_MARGIN_PCT=50`: skip if projected initial margin usage exceeds 50% of balance.
  - `MIN_LIQ_DISTANCE_PCT=20`: skip if any open position is within 20% of liquidation.
  - `MAX_ACCOUNT_NOTIONAL_PCT=80`: skip if total |notional| + new grid exceeds 80% of `wallet × leverage`.
  - `RISK_USE_FULL_GRID=true` (default): checks use full-grid notional, not entry only.
  - Lighter-side opens still allowed for imbalance; `--force` overrides all guards.
- **Entry size**: 10% of wallet (`WALLET_PCT=10`) or fixed `BASE_SIZE` in USDT.
- **Leverage**: symbol max set automatically (`--no-max-leverage` / `--set-leverage N`).
- **DCA walls**: real order-book levels within `--max-range` % (default 12).
- **Trailing TP**: opposite-side wall, activation clamped so callback stays in profit.
- **Order expiry**: only the **base/entry** LIMIT uses **GTD** (`ORDER_TTL=3600` = 1 h; `0` = all GTC). DCA safety orders stay **GTC**. If the entry expires unfilled, `--supervise` cancels the whole grid and re-arms.
- **Grid refresh**: while flat, `--supervise` cancels and re-arms after `GRID_TTL` (default **1 h**; `0` = off), same as Spot. With an open position, DCA orders are kept (use `--rearm` to replenish).
- **Safety**: refuses to stack on existing exposure (`--force` to override); cancels foreign SLs (`--keep-sl` to disable).

Run `python3 orderbook_dca_grid.py --help` for all flags.

---

## Staged exit (default)

Staged exit is the **default** exit mode (code default + `EXIT_MODE=staged` in `.env`). Logic lives in `exits/staged.py` (implementation in `orderbook_staged_exit.py`). Use **one process per symbol**:

```bash
python3 orderbook_dca_grid.py 1000RATSUSDT --supervise
python3 orderbook_dca_grid.py LINKUSDT --audit
python3 orderbook_staged_exit.py LINKUSDT --audit --cleanup   # optional standalone audit
```

When a position is open:

1. Places **TP1 (70%)** as TAKE_PROFIT at **+TP1_PROFIT_PCT** (default **0.3%**) — DCA grid stays active
2. On TP1 fill: **cancels DCA**, **SL on runner at entry + BE_PROFIT_PCT** (default **0.1%** profit lock)
3. **Trailing** on the opposite order-book wall for the runner — **profit-lock SL stays active** alongside the trail

**Do not** run `orderbook_staged_exit.py` in parallel on the same symbol as `--supervise`.

Legacy two-process mode (avoid):

```bash
python3 orderbook_dca_grid.py LINKUSDT --supervise --exit none   # or --no-tp
python3 orderbook_staged_exit.py LINKUSDT
```

| Flag / env | Default | Description |
|------------|---------|-------------|
| `EXIT_MODE` | `staged` | `staged` \| `trailing` \| `none` |
| `--exit staged` | *(env default)* | Staged exit plugin |
| `--exit trailing` | — | Trailing TP @ OB wall |
| `--exit none` / `--no-tp` | — | Grid only |
| `DIRECTION` | `auto` | `auto` \| `long` \| `short` |
| `RECV_WINDOW` | `15000` | Binance recvWindow ms |
| `TP1_PROFIT_PCT` | `0.3` | First partial trigger (%) |
| `BE_PROFIT_PCT` | `0.1` | Runner SL profit lock after TP1 (%) |
| `TP_PARTIAL_PCT` | `70` | First partial size (%) |
| `STAGED_POLL_SEC` | `5` | Staged state poll (via grid `--tp-poll-sec` loop) |

Add new exit strategies under `exits/` and register them in `exits/__init__.py`.

### Telegram alerts + remote control

Uses `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`. If unset, alerts and remote control are skipped.

**Alerts** (`telegram_notify.py`): supervise start, grid/DCA re-arm, DCA fill, position open/close (with PnL), staged TP1/SL/trail, supervisor errors.

**Remote control** (`telegram_botctl.py` + `botctl.py`): start/stop/status supervisors **without closing positions or cancelling orders** on stop.

#### Telegram commands

Only messages from `TELEGRAM_CHAT_ID` are accepted.

| Command | Action |
|---------|--------|
| `/start SYMBOL` | Start supervisor for symbol |
| `/stop SYMBOL` | Stop supervisor (orders & position stay on Binance) |
| `/status SYMBOL` | Process state + position + staged phase + PnL |
| `/list` | All running supervisors |
| `/help` | Command list |

`/start` only works for symbols listed in `FUTURES_PAIRS` (whitelist). `/stop` and `/status` work for any symbol.

CLI equivalent:

```bash
python3 botctl.py start SXTUSDT
python3 botctl.py stop SXTUSDT
python3 botctl.py status SXTUSDT
python3 botctl.py list
```

#### Local (Mac) — manual daemon

```bash
python3 telegram_botctl.py
```

Uses **pidfile** backend: PIDs in `.run/pids/`, logs in `.run/logs/`. Supervisors started with `/start` run in the background.

#### Production (VPS) — systemd

Install once:

```bash
sudo cp deploy/dca-telegram-ctl.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-telegram-ctl
```

Uses **systemd** backend: `/start` and `/stop` call `systemctl` on `dca-futures@SYMBOL`.

```bash
sudo systemctl status dca-telegram-ctl
sudo journalctl -u dca-telegram-ctl -f
```

Optional env: `BOTCTL_MODE=auto|systemd|pidfile`, `FUTURES_UNIT=dca-futures`.

#### Important: Telegram ctl ≠ fleet auto-start

Starting `dca-telegram-ctl` **does not** start trading bots. It only listens for commands.

| Tool | Starts all `FUTURES_PAIRS`? |
|------|---------------------------|
| `python3 deploy/sync_pairs.py` | **Yes** — enable + start every listed symbol |
| `telegram_botctl` `/start SYMBOL` | **One** symbol at a time |
| `/list` | Shows what's **already running** (empty until sync or `/start`) |

After first deploy, run `sync_pairs.py` once to bring up the default fleet (see [Production deploy](#production-deploy-runbook)).

---

## Spot (`orderbook_dca_grid_spot.py`)

Runs on **Binance Spot** (`api.binance.com`). Same `.env`; API key needs **Spot & Margin Trading** permission. **Long-only.**

### Modes

```bash
python3 orderbook_dca_grid_spot.py BTCUSDT --dry-run    # preview
python3 orderbook_dca_grid_spot.py BTCUSDT              # grid + OCO
python3 orderbook_dca_grid_spot.py BTCUSDT --supervise  # autonomous
python3 orderbook_dca_grid_spot.py BTCUSDT --tp-only    # OCO only
python3 orderbook_dca_grid_spot.py SOLUSDT --rearm      # DCA-only if holding; full grid if flat
python3 orderbook_dca_grid_spot.py BNBUSDT --rearm --rearm-flat  # sell holding, then full grid
python3 orderbook_dca_grid_spot.py BTCUSDT --tp-only --once   # sync OCO once and exit
```

### Key behavior

- **BUY LIMIT** grid on real **bid walls**; DCA count from the book (`SO_WALL_MULT`), capped by `SO_MAX`.
- **OCO SELL** while holding: TP on ask walls, SL **below the deepest open DCA** (`SPOT_SL` is fallback when grid is fully filled).
- **Budget fit**: before placing, sums grid notional vs free USDT and `MAX_SYMBOL_PCT` (default **25%** of wallet); drops deepest DCAs until it fits.
- **Grid refresh**: `--supervise` cancels and re-arms after `GRID_TTL` (default **1 h**; `0` = off).

Run `python3 orderbook_dca_grid_spot.py --help` for all flags.

---

## Configuration (`.env`)

Copy `.env.example` → `.env`. CLI flags override env vars.

### Required (production)

```env
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
FUTURES_PAIRS=1000SHIBUSDT,ATOMUSDT,SXTUSDT,...   # fleet + /start whitelist
```

Futures API key needs **Futures trading** permission. `chmod 600 .env`.

### Telegram (alerts + remote control)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Futures defaults (optional — already coded as defaults)

```env
EXIT_MODE=staged          # staged | trailing | none
DIRECTION=auto            # auto | long | short
RECV_WINDOW=15000         # raise if you see -1021 timestamp errors
WALLET_PCT=10
MAX_IMBALANCE=20          # 0 = off
# MAX_MARGIN_PCT=50
# MIN_LIQ_DISTANCE_PCT=20
# MAX_ACCOUNT_NOTIONAL_PCT=80
# RISK_USE_FULL_GRID=true
ORDER_TTL=3600
GRID_TTL=3600
REARM_BACKOFF=60
# TP1_PROFIT_PCT=0.3
# TP_PARTIAL_PCT=70
# TELEGRAM_MIN_OPEN_VOL=5
# BOTCTL_MODE=auto
# FUTURES_UNIT=dca-futures
```

### Full reference

| Variable | Default | Applies to | Description |
|----------|---------|------------|-------------|
| `BINANCE_API_KEY` | — | both | API key |
| `BINANCE_SECRET_KEY` | — | both | Secret |
| `EXIT_MODE` | `staged` | futures | Exit plugin: `staged`, `trailing`, `none` |
| `DIRECTION` | `auto` | futures | Grid direction: `auto`, `long`, `short` |
| `RECV_WINDOW` | `15000` | futures | Binance recvWindow (ms) |
| `WALLET_PCT` | `10` | both | Entry size as % of wallet/free USDT |
| `BASE_SIZE` | `0` | both | Fixed entry USDT (`0` = use `WALLET_PCT`) |
| `MAX_IMBALANCE` | `20` | futures | Account LONG/SHORT balance guard (`0` = off) |
| `MAX_MARGIN_PCT` | `50` | futures | Max projected initial margin / balance (`0` = off) |
| `MIN_LIQ_DISTANCE_PCT` | `20` | futures | Min distance to liq on any position (`0` = off) |
| `MAX_ACCOUNT_NOTIONAL_PCT` | `80` | futures | Cap on total \|notional\| + grid vs wallet×lev |
| `RISK_USE_FULL_GRID` | `true` | futures | Risk checks use full grid notional |
| `ORDER_TTL` | `3600` | futures | Entry order GTD in seconds; DCA orders stay GTC (`0` = all GTC) |
| `GRID_TTL` | `3600` | futures + spot | Refresh stale flat grid (seconds; `0` = off) |
| `REARM_BACKOFF` | `60` | both | Wait when flat but grid can't be armed |
| `TP1_PROFIT_PCT` | `0.3` | futures staged | First partial trigger (%) |
| `BE_PROFIT_PCT` | `0.1` | futures staged | Runner SL profit lock after TP1 (%) |
| `TP_PARTIAL_PCT` | `70` | futures staged | First partial size (%) |
| `TELEGRAM_BOT_TOKEN` | — | telegram | Bot token for alerts + remote control |
| `TELEGRAM_CHAT_ID` | — | telegram | Allowed chat for alerts + commands |
| `TELEGRAM_MIN_OPEN_VOL` | `5` | telegram | Min notional USDT to send `#OPEN` alert |
| `BOTCTL_MODE` | `auto` | telegram ctl | `auto`, `systemd`, or `pidfile` |
| `FUTURES_UNIT` | `dca-futures` | deploy / botctl | systemd template name |
| `FUTURES_PAIRS` | — | deploy | Comma-separated symbols for `dca-futures@` |
| `SPOT_PAIRS` | — | deploy | Comma-separated symbols for `dca-spot@` |
| `MAX_SYMBOL_PCT` | `25` | spot | Cap per symbol (% of total USDT wallet) |
| `MIN_BASE_USDT` | `10` | spot | Floor for wallet-% entry size |
| `SPOT_TP` / `SPOT_SL` | `0.5` / `5` | spot | OCO TP/SL % |

Example pair lists (alphabetical):

```env
FUTURES_PAIRS=1000SHIBUSDT,ATOMUSDT,AVAXUSDT,DOGEUSDT,EIGENUSDT,ETCUSDT,NEARUSDT,OPUSDT,SUIUSDT,XRPUSDT
SPOT_PAIRS=BNBUSDT,BTCUSDT,ETHUSDT,SOLUSDT
```

---

## 24/7 on Ubuntu (systemd)

### Managed setup (REVISION ALPHA)

If you prefer not to configure the server yourself, you can hire a VPS through **[REVISION ALPHA S.L.](https://revisionalpha.com/servidores-dedicados)** — we install this bot on your server **free of charge** (clone, `.env`, systemd units, and `sync_pairs.py`).

Contact: [info@revisionalpha.com](mailto:info@revisionalpha.com) · [+34 613 194 131](tel:+34613194131)

Install path (code under `/opt`, process runs as **`forge`** — never as root):

```text
/opt/trade-binance-websocket-orderbook-dca-grid
```

The `.env` is **not** in git — create it on the server. Units use `User=forge` and `WorkingDirectory` pointing at the project folder.

### Fresh install (new server)

```bash
# 1. Clone as forge
sudo mkdir -p /opt
sudo git clone https://github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid.git \
  /opt/trade-binance-websocket-orderbook-dca-grid
sudo chown -R forge:forge /opt/trade-binance-websocket-orderbook-dca-grid

# 2. Branch + secrets
cd /opt/trade-binance-websocket-orderbook-dca-grid
git checkout dev
cp .env.example .env
chmod 600 .env
nano .env   # see Configuration section above

# 3. Dry-run
python3 orderbook_dca_grid.py BTCUSDT --dry-run

# 4. Install systemd units (once)
sudo cp deploy/dca-futures@.service deploy/dca-spot@.service \
        deploy/dca-telegram-ctl.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Start Telegram remote control (optional)
sudo systemctl enable --now dca-telegram-ctl

# 6. Start trading fleet from FUTURES_PAIRS
python3 deploy/sync_pairs.py --dry-run
python3 deploy/sync_pairs.py
python3 deploy/sync_pairs.py status
```

### Production deploy runbook

What to run after each type of change:

| Situation | Commands |
|-----------|----------|
| **First install** | Steps above (units + `sync_pairs.py` + optional `dca-telegram-ctl`) |
| **Code update** (`git pull`) | `git pull` then `python3 deploy/sync_pairs.py --restart` |
| **Telegram ctl code only** | `sudo systemctl restart dca-telegram-ctl` |
| **`.env` changed (same pairs)** | `sync_pairs.py --restart` (reload config in running bots) |
| **Add/remove symbol in `FUTURES_PAIRS`** | Edit `.env`, then `python3 deploy/sync_pairs.py` (starts new, stops removed) |
| **One symbol manually** | Telegram `/start SYMBOL` or `/stop SYMBOL` |

`git pull` alone does **not** restart bots — they keep old code in memory until `--restart`.

Typical deploy:

```bash
cd /opt/trade-binance-websocket-orderbook-dca-grid
git checkout dev && git pull
python3 deploy/sync_pairs.py --restart
sudo systemctl restart dca-telegram-ctl
```

### systemd units

| Unit | Command | Use when |
|------|---------|----------|
| `dca-futures@SYMBOL` | `--supervise` | **Main bot**: grid + staged exit (defaults from `.env`/code) |
| `dca-telegram-ctl` | `telegram_botctl.py` | Telegram `/start` `/stop` `/status` (24/7) |
| `dca-futures-tp@SYMBOL` | `--tp-only` | Exit only (manual/other entry) |
| `dca-spot@SYMBOL` | `--supervise` | Spot grid + OCO |

Do **not** run `dca-futures@` and `dca-staged-exit@` on the same symbol. Staged exit is built into `dca-futures@` via `EXIT_MODE=staged`.

Do **not** run `dca-futures@` and `dca-futures-tp@` on the same symbol.

### Sync pairs from `.env`

```bash
python3 deploy/sync_pairs.py status      # desired vs running
python3 deploy/sync_pairs.py --dry-run   # preview
python3 deploy/sync_pairs.py             # enable + start listed, disable the rest
python3 deploy/sync_pairs.py --restart   # same + restart already-running units
```

Omit `FUTURES_PAIRS` or `SPOT_PAIRS` to leave that market untouched. An empty value disables all units for that template.

### Logs

```bash
# Trading bots
sudo journalctl -u 'dca-futures@*' -u 'dca-spot@*' -f -o with-unit

# Telegram remote control
sudo journalctl -u dca-telegram-ctl -f

# One symbol
sudo journalctl -u dca-futures@SXTUSDT -n 50
```

Mac (pidfile backend): `.run/logs/SYMBOL.log`

### `/list` says "No supervisors running" but Telegram shows trades

`/list` only sees **systemd units** named `dca-futures@SYMBOL` (or legacy `dca-super@`). It is **not** the same as "positions open on Binance".

Common causes:

| Cause | What happened | Fix |
|-------|----------------|-----|
| **sudo without TTY** | `dca-telegram-ctl` runs as `forge`; `sudo systemctl` fails silently in the daemon | Updated `botctl.py` tries systemctl **without sudo** for reads. Redeploy + `sudo systemctl restart dca-telegram-ctl`. For `/start`/`/stop`, add passwordless sudo for forge: `forge ALL=(ALL) NOPASSWD: /bin/systemctl *` |
| **Fleet never synced** | Alerts from a one-off run; units not enabled | `python3 deploy/sync_pairs.py` on the VPS |
| **Old unit names** | Bots under `dca-super@` not `dca-futures@` | Migrate per section below, or set `FUTURES_UNIT=dca-super` in `.env` |
| **Supervisor crashed** | Last alert minutes ago; position still open | `sudo journalctl -u 'dca-futures@*' -n 30`; restart with `/start SYMBOL` or `sync_pairs.py --restart` |

Check on the VPS:

```bash
python3 deploy/sync_pairs.py status
sudo systemctl list-units 'dca-futures@*' --state=active
pgrep -af 'orderbook_dca_grid.py.*--supervise'
```

### Migrate from `dca-super@` / `dca-tp@` (old unit names)

```bash
sudo systemctl disable --now 'dca-super@*' 'dca-tp@*'
sudo cp deploy/dca-futures@.service deploy/dca-futures-tp@.service /etc/systemd/system/
sudo systemctl daemon-reload
python3 deploy/sync_pairs.py
```

---

## Disclaimer

This places **real orders** on your Binance Futures **and/or Spot** account. Test with `--dry-run` and small size first. No warranty.

## Security Vulnerabilities

If you discover a security vulnerability, please e-mail [hola@idoneo.dev](mailto:hola@idoneo.dev).

## License

Licensed under the [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html).

### Additional Terms

By deploying this software, you agree to notify the original author at [hola@idoneo.dev](mailto:hola@idoneo.dev) or via [LinkedIn](http://linkedin.com/in/diego-mascarenhas/). Any modifications or enhancements must be shared with the original author.
