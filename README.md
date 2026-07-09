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
├── orderbook_dca_grid.py   # the bot (single module, contains main())
├── pyproject.toml          # optional install → `orderbook-dca-grid` command
├── .env.example            # API key template
└── deploy/                 # systemd units for Ubuntu (24/7)
    ├── dca-tp@.service
    └── dca-super@.service
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
- **Balance filter**: `--max-imbalance 30` (default) skips opening when bid vs ask
  volume near the mid differ by more than that % (e.g. `790` vs `116` ≈ 74% → skipped).
  Set `0` to disable, or `--force` to override. Also configurable via `.env`
  (`MAX_IMBALANCE`).
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

sudo cp deploy/dca-super@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-super@ADAUSDT      # autonomous per symbol
# or, TP-only (manage an existing position, no grid):
# sudo systemctl enable --now dca-tp@SOLUSDT
```

Operate / logs (journald):

```bash
systemctl status dca-super@ADAUSDT
journalctl -u dca-super@ADAUSDT -f
sudo systemctl restart dca-super@ADAUSDT
sudo systemctl disable --now dca-super@ADAUSDT
```

> If your path/user differ, edit `WorkingDirectory`/`ExecStart`/`User` in the
> `.service` file. Use `dca-super@` (autonomous) **or** `dca-tp@` (manage only)
> per symbol — not both.

## Disclaimer

This places **real orders** on your Binance Futures account. Test with small size
first. No warranty.

## Security Vulnerabilities

If you discover a security vulnerability within trade-binance-websocket-orderbook-dca-grid, please send an e-mail to Diego Mascarenhas Goytía via [hola@idoneo.dev](mailto:hola@idoneo.dev). All security vulnerabilities will be promptly addressed.

## License

trade-binance-websocket-orderbook-dca-grid is open-sourced software licensed under the [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)

### Additional Terms

By deploying this software, you agree to notify the original author at [hola@idoneo.dev](mailto:hola@idoneo.dev). or by visiting [http://linkedin.com/in/diego-mascarenhas/](http://linkedin.com/in/diego-mascarenhas/) Any modifications or enhancements must be shared with the original author.
