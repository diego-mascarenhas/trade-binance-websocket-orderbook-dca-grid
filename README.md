# trade-binance-websocket-orderbook-dca-grid

Order-book anchored **DCA grid** bots for **Binance Futures USDT-M** and **Binance Spot**.

Repository: [github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid](https://github.com/diego-mascarenhas/trade-binance-websocket-orderbook-dca-grid)

Both scripts read the live order book, place DCA orders on real walls, size entries from wallet %, and manage exits anchored to the book:

| Bot | Market | Entry | Exit |
|-----|--------|-------|------|
| `orderbook_dca_grid.py` | Futures | LONG/SHORT grid on walls | Trailing TP (`TRAILING_STOP_MARKET`) |
| `orderbook_dca_grid_spot.py` | Spot | BUY grid on bid walls | OCO (TP + SL) |

Self-contained: **Python standard library only** — no third-party dependencies.

## Project layout

```
trade-binance-websocket-orderbook-dca-grid/
├── orderbook_dca_grid.py        # Futures bot
├── orderbook_dca_grid_spot.py   # Spot bot
├── pyproject.toml               # optional install → `orderbook-dca-grid` command
├── .env.example                 # API keys + config template
└── deploy/
    ├── dca-futures@.service     # Futures: grid + trailing TP (--supervise)
    ├── dca-futures-tp@.service  # Futures: trailing TP only (--tp-only)
    ├── dca-spot@.service        # Spot: buy grid + OCO (--supervise)
    └── sync_pairs.py            # enable/disable systemd units from .env pair lists
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

# Fully autonomous: re-arm grid when flat + manage TP
python3 orderbook_dca_grid.py ADAUSDT --supervise

# Cancel + fresh grid (DCA-only if holding; stop systemd unit first)
python3 orderbook_dca_grid.py OPUSDT --rearm --dry-run
python3 orderbook_dca_grid.py OPUSDT --rearm
python3 orderbook_dca_grid.py OPUSDT --rearm --rearm-flat  # close position, then full grid
```

### Key behavior

- **Direction**: `--direction auto` (default) from bid/ask imbalance; or `long` / `short`.
- **Account balance guard**: `MAX_IMBALANCE=30` (default) skips new orders on the heavier side when `|LONG − SHORT| / total` exceeds the limit; the lighter side is still allowed. `--force` overrides.
- **Entry size**: 10% of wallet (`WALLET_PCT=10`) or fixed `BASE_SIZE` in USDT.
- **Leverage**: symbol max set automatically (`--no-max-leverage` / `--set-leverage N`).
- **DCA walls**: real order-book levels within `--max-range` % (default 12).
- **Trailing TP**: opposite-side wall, activation clamped so callback stays in profit.
- **Order expiry**: only the **base/entry** LIMIT uses **GTD** (`ORDER_TTL=3600` = 1 h; `0` = all GTC). DCA safety orders stay **GTC**. If the entry expires unfilled, `--supervise` cancels the whole grid and re-arms. With an open position, DCA orders are kept.
- **Safety**: refuses to stack on existing exposure (`--force` to override); cancels foreign SLs (`--keep-sl` to disable).

Run `python3 orderbook_dca_grid.py --help` for all flags.

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
- **Grid refresh**: Spot has no native GTD — `--supervise` cancels and re-arms after `GRID_TTL` (default **1 h**; `0` = off).

Run `python3 orderbook_dca_grid_spot.py --help` for all flags.

---

## Configuration (`.env`)

Copy `.env.example` → `.env`. CLI flags override env vars.

| Variable | Default | Applies to | Description |
|----------|---------|------------|-------------|
| `BINANCE_API_KEY` | — | both | API key |
| `BINANCE_SECRET_KEY` | — | both | Secret |
| `WALLET_PCT` | `10` | both | Entry size as % of wallet/free USDT |
| `BASE_SIZE` | `0` | both | Fixed entry USDT (`0` = use `WALLET_PCT`) |
| `MAX_IMBALANCE` | `30` | futures | Account LONG/SHORT balance guard (`0` = off) |
| `ORDER_TTL` | `3600` | futures | Entry order GTD in seconds; DCA orders stay GTC (`0` = all GTC) |
| `REARM_BACKOFF` | `60` | both | Wait when flat but grid can't be armed |
| `MAX_SYMBOL_PCT` | `25` | spot | Cap per symbol (% of total USDT wallet) |
| `MIN_BASE_USDT` | `10` | spot | Floor for wallet-% entry size |
| `GRID_TTL` | `3600` | spot | Refresh stale armed grid (seconds) |
| `SPOT_TP` / `SPOT_SL` | `0.5` / `5` | spot | OCO TP/SL % |
| `FUTURES_PAIRS` | — | deploy | Comma-separated symbols for `dca-futures@` |
| `SPOT_PAIRS` | — | deploy | Comma-separated symbols for `dca-spot@` |

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

# 2. Secrets
cd /opt/trade-binance-websocket-orderbook-dca-grid
cp .env.example .env
chmod 600 .env
nano .env   # BINANCE_API_KEY, BINANCE_SECRET_KEY, FUTURES_PAIRS, SPOT_PAIRS

# 3. Dry-run
python3 orderbook_dca_grid_spot.py BTCUSDT --dry-run

# 4. systemd (needs sudo for systemctl)
sudo cp deploy/dca-futures@.service deploy/dca-futures-tp@.service deploy/dca-spot@.service /etc/systemd/system/
sudo systemctl daemon-reload
python3 deploy/sync_pairs.py --dry-run
python3 deploy/sync_pairs.py
```

Updates later:

```bash
cd /opt/trade-binance-websocket-orderbook-dca-grid
git pull
python3 deploy/sync_pairs.py --restart
```

| Unit | Command | Use when |
|------|---------|----------|
| `dca-futures@SYMBOL` | `--supervise` | Full bot: grid + trailing TP |
| `dca-futures-tp@SYMBOL` | `--tp-only` | Exit only (manual/other entry) |
| `dca-spot@SYMBOL` | `--supervise` | Spot grid + OCO |

Do **not** run `dca-futures@` and `dca-futures-tp@` on the same symbol.

### Sync pairs from `.env`

```bash
python3 deploy/sync_pairs.py status     # desired vs running
python3 deploy/sync_pairs.py --dry-run  # preview
python3 deploy/sync_pairs.py            # enable+start listed, disable the rest
python3 deploy/sync_pairs.py --restart  # same + restart running units
```

Omit `FUTURES_PAIRS` or `SPOT_PAIRS` to leave that market untouched. An empty value disables all units for that template.

### Logs

```bash
sudo journalctl -u 'dca-futures@*' -u 'dca-spot@*' -f -o with-unit
sudo systemctl restart 'dca-futures@*' 'dca-spot@*'
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
