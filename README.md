# trade-binance-websocket-orderbook-dca-grid

Order-book anchored **DCA grid + trailing take-profit** bot for **Binance Futures
USDT-M**. It reads the live order book, places a base order plus safety orders
(DCA) on real walls, sizes the entry as a % of your wallet, uses the symbol's max
leverage, and manages a profit-guaranteed trailing take-profit on the opposite
side of the book.

Self-contained: **Python standard library only** — no third-party dependencies.

## Project layout

```
trade-binance-websocket-orderbook-dca-grid/
├── orderbook_dca_grid.py        # the Futures bot (single module, contains main())
├── orderbook_dca_grid_spot.py   # the Spot bot (buy-grid + OCO TP/SL)
├── pyproject.toml               # optional install → `orderbook-dca-grid` command
├── .env.example                 # API key template
└── deploy/                      # systemd units for Ubuntu (24/7)
    ├── dca-futures@.service     # Futures: grid + trailing TP (--supervise)
    ├── dca-futures-tp@.service  # Futures: trailing TP only (--tp-only)
    ├── dca-spot@.service        # Spot: buy grid + OCO
    └── sync_pairs.py            # enable/disable units from .env pair lists
```

## Setup

```bash
cp .env.example .env      # then fill in BINANCE_API_KEY / BINANCE_SECRET_KEY
```

Keys are read from environment variables first, then `.env` (cwd or next to the
script). No `pip install` is required to run.

## Run

It **executes by default** (auto-direction, 5% wallet, max leverage, DCA walls
within ±12%). Add `--dry-run` to only preview.

```bash
# directly
python3 orderbook_dca_grid.py ADAUSDT

# or as a module
python3 -m orderbook_dca_grid ADAUSDT
```

Optional install (adds an `orderbook-dca-grid` command on your PATH):

```bash
pip install .
orderbook-dca-grid ADAUSDT
```

Preview without sending anything:

```bash
python3 orderbook_dca_grid.py ADAUSDT --dry-run
```

## Modes

```bash
# Place grid once + auto-manage the trailing TP (foreground loop) — default
python3 orderbook_dca_grid.py ADAUSDT

# Only manage the trailing TP for an existing position (no new grid)
python3 orderbook_dca_grid.py ADAUSDT --tp-only

# Fully autonomous: re-arm the grid whenever flat + manage TP
python3 orderbook_dca_grid.py ADAUSDT --supervise
```

## Key behavior / defaults

- **Direction**: `--direction auto` (default) decides long/short from bid/ask
  imbalance; or pass `--direction long|short`.
- **Exposure balance**: `--max-imbalance 30` (default) keeps your account's total
  LONG vs SHORT notional within that % of each other (`|LONG - SHORT| / total`).
  Before opening, if adding to the heavier side would exceed it, that side is
  skipped; the lighter side is still allowed (it rebalances). E.g. LONG `748` vs
  SHORT `349` ≈ 36% → new LONG skipped. Set `0` to disable, `--force` to override.
  Also configurable via `.env` (`MAX_IMBALANCE`).
- **Entry size**: 10% of wallet balance (`--wallet-pct 10`); or fixed `--base-size 20`.
  Both can also be set in `.env` (`WALLET_PCT`, `BASE_SIZE`); CLI flags take precedence.
- **Leverage**: the symbol's max is set automatically (`--no-max-leverage` to skip,
  `--set-leverage N` to force).
- **DCA walls**: anchored to real order-book walls within `--max-range` % (default 12).
- **Execution**: on by default; pass `--dry-run` to preview without sending orders.
- **Trailing TP**: `TRAILING_STOP_MARKET` on the opposite wall, activation clamped
  so the callback stays in profit; only replaced when the position size changes.
- **Safety**: refuses to place a grid if the symbol already has a position/orders
  (`--force` to override); auto-cancels foreign SLs (`--keep-sl` to disable).
- **Order expiry (Futures)**: LIMIT grid orders use native **GTD** by default
  (`ORDER_TTL=3600` = 1 h in `.env`; `0` = GTC via `--tif`). Binance cancels
  unfilled limits automatically; `--supervise` re-arms when flat.

Run `python3 orderbook_dca_grid.py --help` for the full flag list.

## 24/7 on Ubuntu

The project is synced (via SFTP) to
`/home/forge/scripts/trade-binance-websocket-orderbook-dca-grid`. The `.env` is
**not** uploaded, so create it once on the server; the systemd unit sets
`WorkingDirectory` to the project folder and the script reads keys from that `.env`.

```bash
cd /home/forge/scripts/trade-binance-websocket-orderbook-dca-grid
cp .env.example .env       # fill in BINANCE_API_KEY / BINANCE_SECRET_KEY
chmod 600 .env

sudo cp deploy/dca-futures@.service deploy/dca-futures-tp@.service deploy/dca-spot@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-futures@ADAUSDT   # grid + trailing TP per symbol
# or, TP-only (manage an existing position, no grid):
# sudo systemctl enable --now dca-futures-tp@SOLUSDT
```

Operate / logs (journald):

```bash
systemctl status dca-futures@ADAUSDT
journalctl -u 'dca-futures@*' -f -o with-unit
sudo systemctl restart dca-futures@ADAUSDT
sudo systemctl disable --now dca-futures@ADAUSDT
```

### Migrate from `dca-super@` / `dca-tp@` (old names)

```bash
sudo systemctl disable --now 'dca-super@*' 'dca-tp@*'
sudo cp deploy/dca-futures@.service deploy/dca-futures-tp@.service /etc/systemd/system/
sudo systemctl daemon-reload
python3 deploy/sync_pairs.py     # or enable units manually
```

### Sync pairs from `.env`

Edit `FUTURES_PAIRS` and/or `SPOT_PAIRS` in `.env`, then run:

```bash
python3 deploy/sync_pairs.py status    # desired vs running
python3 deploy/sync_pairs.py --dry-run # preview
python3 deploy/sync_pairs.py           # enable+start listed, disable the rest
python3 deploy/sync_pairs.py --restart # same + restart already-running units
```

Example `.env`:

```env
FUTURES_PAIRS=1000SHIBUSDT,ATOMUSDT,AVAXUSDT,DOGEUSDT,NEARUSDT,OPUSDT,SUIUSDT,XRPUSDT
SPOT_PAIRS=BNBUSDT,BTCUSDT,ETHUSDT,SOLUSDT
```

Omit `FUTURES_PAIRS` or `SPOT_PAIRS` entirely to leave that market untouched.
An empty value (`SPOT_PAIRS=`) disables all units for that template.

> If your path/user differ, edit `WorkingDirectory`/`ExecStart`/`User` in the
> `.service` file. Use `dca-futures@` (grid + TP) **or** `dca-futures-tp@` (TP only)
> per symbol — not both.

## Spot variant (`orderbook_dca_grid_spot.py`)

A sibling bot for **Binance Spot** (`api.binance.com`), using the **same `.env`**
(the key just needs *Spot & Margin Trading* permission). Spot is **long-only**, so
it works differently from the Futures bot:

- Places **BUY LIMIT** orders on real **bid walls** below entry to DCA the dip.
  The **number of DCA is detected from the order book** (a level counts as a wall
  when its size ≥ `--so-wall-mult`× the median book size), not a fixed target;
  `--so-count` is only a safety **cap** (default 15, `0` = no cap).
- While holding the asset, it maintains a single **OCO SELL** exit, also
  **anchored to the order book**:
  - **Take-profit** (`LIMIT_MAKER`): sits on a real ask wall (resistance) at/above
    the profit floor `avg*(1+tp%)` (`--tp`, default +0.5%).
  - **Stop-loss** (`STOP_LOSS_LIMIT`): placed **below the whole DCA grid** — under
    the deepest still-open DCA order, snapped beneath a support wall with a
    `--sl-buffer` cushion (default 0.5%). This lets the grid buy the dips before
    ever cutting. `--sl` (default -5%) is only the **fallback** distance below avg
    once the grid is fully filled (no open DCA left).
  - One leg cancels the other automatically. Tune wall detection with
    `--tp-wall-min-mult` / `--tp-wall-pick`.
- Enters with **`--wallet-pct`% of your free USDT** (default 10%); or a fixed
  `--base-size N`. Deep defaults (`--limit 5000`, `--max-range 15`) so it finds
  walls on pricey coins (e.g. ETH) without extra flags.
- No leverage / no shorting / no hedge (they don't exist on Spot).
- Exposure guard per symbol: `--max-symbol-pct` caps **held + full grid cost** as a
  **% of the wallet** (total USDT, default **25%**), or `--max-symbol-usdt` as an
  absolute amount (used only when pct=0). `0` on both = off. Before placing, the bot
  sums the rounded grid notional, compares it to **free USDT** and the cap, and
  **drops the deepest DCA orders** until the grid fits (keeps base + nearest DCAs).
  Env: `MAX_SYMBOL_PCT` / `MAX_SYMBOL_USDT`.
- **Grid refresh (Spot)**: no native GTD on spot LIMIT orders — `--supervise`
  cancels and re-arms stale grids after `GRID_TTL` seconds (default **1 h**;
  `0` = off). Env: `GRID_TTL`.
- Avg cost is derived from your recent buy trades (`myTrades`) to anchor the TP/SL.

```bash
# preview first (recommended) — no orders sent
python3 orderbook_dca_grid_spot.py ADAUSDT --dry-run

# place the buy grid + auto-manage the OCO exit
python3 orderbook_dca_grid_spot.py ADAUSDT

# fully autonomous: re-arm the grid when flat + keep the OCO synced
python3 orderbook_dca_grid_spot.py ADAUSDT --supervise

# only (re)place/manage the OCO for what you already hold
python3 orderbook_dca_grid_spot.py ADAUSDT --tp-only

# cancel open orders and place a fresh buy grid (stop the systemd unit first)
python3 orderbook_dca_grid_spot.py SOLUSDT --rearm
python3 orderbook_dca_grid_spot.py BNBUSDT --rearm --rearm-flat   # sell first, start flat
```

Env knobs (in `.env`): `SPOT_TP`, `SPOT_SL`, `MAX_SYMBOL_USDT`, plus the shared
`WALLET_PCT` / `BASE_SIZE` / `REARM_BACKOFF`. 24/7 on Ubuntu with the
`dca-spot@` unit:

```bash
sudo cp deploy/dca-spot@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-spot@ADAUSDT
sudo journalctl -u 'dca-spot@*' -f -o with-unit
```

## Disclaimer

This places **real orders** on your Binance Futures **and/or Spot** account. Test
with small size first (`--dry-run`). No warranty.

## Security Vulnerabilities

If you discover a security vulnerability within trade-binance-websocket-orderbook-dca-grid, please send an e-mail to Diego Mascarenhas Goytía via [hola@idoneo.dev](mailto:hola@idoneo.dev). All security vulnerabilities will be promptly addressed.

## License

trade-binance-websocket-orderbook-dca-grid is open-sourced software licensed under the [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)

### Additional Terms

By deploying this software, you agree to notify the original author at [hola@idoneo.dev](mailto:hola@idoneo.dev). or by visiting [http://linkedin.com/in/diego-mascarenhas/](http://linkedin.com/in/diego-mascarenhas/) Any modifications or enhancements must be shared with the original author.
