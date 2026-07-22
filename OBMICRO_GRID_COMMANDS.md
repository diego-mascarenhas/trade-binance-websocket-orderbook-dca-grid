# Micro-grid FIB — full guide

Fibonacci pullback micro-grid for Binance USDⓈ-M futures.

| Item | Path / command |
|------|----------------|
| Bot | `orderbook_micro_grid.py` |
| Short wrapper | `./fib` (also on `PATH` via `~/bin/fib` if installed) |
| Long wrapper | `./obmicro-grid` |
| DCA cousin | `./dca` → `orderbook_dca_grid.py` |
| Journal | `.run/logs/SYMBOL/micro_grid.log` |
| Pidfile | `.run/pids/fib-SYMBOL.pid` |
| Background log (`/fib`) | `.run/logs/fib-SYMBOL.log` |
| Cheat sheet target | this file |

**Stop:** `Ctrl+C` or Telegram `/stop SYMBOL` — does **not** flatten.  
**Flatten:** `./fib SYMBOL --flatten` (cancels grid/exits + market-closes).

---

## Quick start

```bash
# From repo (or anywhere if ~/bin is on PATH)
fib LTCUSDT
fib SKLUSDT short
fib LDOUSDT long --entry-usdt 50
fib LTCUSDT --dry-run
fib LDOUSDT --flatten

# Same bot, all flags through:
./obmicro-grid LDOUSDT --direction short --fib-interval 5m
```

### PATH install (macOS)

Already set up when documented: symlinks in `~/bin` + `export PATH="$HOME/bin:$PATH"` in `~/.zshrc`.

```bash
# New shells see: fib · dca · obmicro-grid
source ~/.zshrc
fib LTCUSDT
```

Wrappers resolve symlinks to the repo root (so `~/bin/fib` finds `orderbook_micro_grid.py`).

---

## Default profile (recommended)

```bash
fib SYMBOL
```

| Setting | Default | Meaning |
|---------|---------|---------|
| Grid | Fib `5m` | Swing TF for impulse |
| Arm window | Fib **0 → 0.236** | Only near impulse extreme |
| Entry | LIMIT pullback | No MARKET (`wait-pullback` on) |
| Levels | `4` | First 4 retrace rungs |
| Size | base `10` / level `8` USDT | Or `--entry-usdt N` |
| Leverage | symbol **max** | Unless `--set-leverage` / `--no-max-leverage` |
| TP | `avg` + **0.30% net** (+0.08% fees) | Refreshed from live avg on every DCA |
| SL | `0.50%` / Fib origin | Fixed until protect trail |
| Protect trail | **ON** | After full fill + profit ≥ callback |
| Cooldown | **3600 s (1h)** | After flat before next arm |
| Direction | `auto` | 15s order-book signal |

---

## Recipes

**Conservative**
```bash
fib SKLUSDT short \
  --entry-usdt 20 \
  --set-leverage 10 \
  --arm-max-fib 0.236 \
  --levels 3 \
  --require-fvg \
  --sl-pct 0.40
```

**Aggressive**
```bash
fib SKLUSDT short \
  --no-wait-pullback \
  --arm-max-fib 1 \
  --entry-usdt 50 \
  --levels 4 \
  --sl-pct 0.50
```

**Paper**
```bash
fib LDOUSDT --dry-run
```

**Fib 1m (faster scalp)**
```bash
fib LDOUSDT long --fib-interval 1m
```

**Hedge: open SHORT while LONG stays**
```bash
fib SKLUSDT short --position-mode hedge
```

**Shorter cooldown**
```bash
fib LTCUSDT --cooldown-sec 300
```

---

## CLI arguments

### Basics

| Flag | Default | Description |
|------|---------|-------------|
| `SYMBOL` | (required) | Futures symbol, e.g. `LDOUSDT` |
| `--execute` / `--no-execute` | execute on | Live orders vs plan only |
| `--dry-run` | off | No orders (overrides execute) |
| `--direction` | `auto` | `auto` \| `long` \| `short` |
| `--position-mode` | `auto` | `auto` \| `hedge` \| `oneway` |
| `--env-file` | `.env` | Path to `.env` |
| `--recv-window` | `15000` | API recv window |
| `--once` | off | Exit after first cycle |
| `--flatten` | off | Cancel grid/exits + market-close, then exit |
| `--cooldown-sec` | `3600` | Wait after flat/cycle before re-arm (1h) |

### Fib / grid

| Flag | Default | Description |
|------|---------|-------------|
| `--grid-mode` | `fib` | `fib` \| `step` |
| `--fib-interval` | `5m` | Swing TF (e.g. `1m` for faster scalp) |
| `--fib-lookback` | `40` | Lookback bars |
| `--fib-min-range` | `0.40` | Min swing range (%) |
| `--fib-max-span` | `12` | Max bars in impulse |
| `--fib-tp-ext` | `1.272` | TP extension (legacy) |
| `--fib-sl-buf` | `0.15` | % buffer beyond origin for SL |
| `--arm-max-fib` | `0.236` | Arm only while depth ≤ this Fib (0→max) |
| `--levels` | `4` | Number of LIMIT rungs placed |
| `--step-pct` | `0.08` | Only for `--grid-mode step` |

### Pullback / arming

| Flag | Default | Description |
|------|---------|-------------|
| `--wait-pullback` / `--no-wait-pullback` | on | LIMIT-only vs immediate MARKET |
| `--raise-top` / `--no-raise-top` | on | Raise Fib/TP if impulse extends (while pending) |
| `--raise-min-pct` | `0.05` | Min % extension to trigger raise |
| `--arm-timeout-sec` | `900` | Disarm if no fill |
| `--require-fvg` / `--no-require-fvg` | off | Hard-require aligned FVG |
| `--fvg-min-pct` | `0.08` | Min FVG height (%) |

### Size / exits / protect

| Flag | Default | Description |
|------|---------|-------------|
| `--entry-usdt` | `0` | Entry notional USDT (sets base; max lev by default) |
| `--base-size` | `10` | First rung notional USDT |
| `--level-size` | `8` | Deeper rung notional USDT |
| `--set-leverage` | `0` | Force leverage (`0` = symbol max) |
| `--no-max-leverage` | off | Do not raise to symbol max |
| `--tp-mode` | `avg` | `avg` = live avg ± (net%+fees), refresh each DCA \| `swing` |
| `--tp-pct` | `0.30` | **Net** take-profit % from average (after fees) |
| `--tp-fee-pct` | `0.08` | Round-trip fee % added on top of `--tp-pct` (gross ≈ 0.38%) |
| `--sl-pct` | `0.50` | SL % from entry/mark |
| `--protect-trail` / `--no-protect-trail` | **on** | Full `--levels` filled + in profit → trailing SL |
| `--protect-trail-callback` | `0.2` | Trailing `callbackRate` % (also min profit before arm) |
| `--protect-arm-pnl-pct` | `0` | Extra min mark profit % (effective min = max(this, callback)) |
| `--sweep` / `--no-sweep` | off | Re-place filled rung further (barrido) |

### 15s signal (`--direction auto` only)

| Flag | Default | Description |
|------|---------|-------------|
| `--bar-sec` | `15` | OB bar length |
| `--sample-sec` | `1` | Book sample interval |
| `--imb-long` | `0.55` | Min imbalance for long |
| `--imb-short` | `0.45` | Max imbalance for short |
| `--momentum-min-pct` | `0.01` | Min bar momentum |
| `--band-pct` | `1.0` | Depth band |
| `--depth-limit` | `50` | Depth levels |

---

## Behavior (lifecycle)

1. **Signal** — 15s OB bar (`auto`) or fixed `--direction`.
2. **Fib plan** — **same-side swing required** (LONG→bull low→high, SHORT→bear high→low). No opposite-swing fallback, no step-grid substitute. Arm only if mark is in Fib **0.000 … `--arm-max-fib`**.
3. **Arm** — place LIMIT grid (default) or MARKET+grid (`--no-wait-pullback`).
4. **First fill** — arm exchange **TP + SL** (`TP = avg ± (0.30% net + 0.08% fees)`, no Fib cap).
5. **More fills (DCA)** — recompute TP from new average (~0.38% gross) and refresh exchange exits; optional Telegram `#FIB FILL`.
6. **Protect trail** (default) — when all `--levels` are filled **and** mark profit ≥ callback %:
   - cancel fixed SL
   - place `TRAILING_STOP_MARKET` (TP kept)
   - table shows `TRAIL` row
7. **Flat** — cleanup grid/exits; Telegram `#FIB CLOSED`; then **cooldown 1h** before next arm.
8. **Adopt** — same-side position already open → rebuild live table (restart-safe). Note: Fib ladder after adopt is rebuilt from current mark (FILLED map can look wrong); trail still arms when conditions hold.

**Ctrl+C / `/stop`:** process stops; **orders and position stay on Binance**.

---

## Live table

Price-ordered ladder (SHORT: low→high). Roles:

| Role | Meaning |
|------|---------|
| `OPEN` | LIMIT still on book |
| `FILLED ✓` | Rung filled |
| `GRID —` | Fib level not placed (`--levels` smaller than full Fib set) |
| `TP` / `SL` | Exchange conditional algos |
| `TRAIL` | Protect trailing armed (`cb=…%`) |
| `▶ MARK` / `▶ ENTRY` | Live mid / average entry |

Only the **ROLE** cell uses color for `GRID`/`ORIGIN` (dim); prices stay normal.

---

## Telegram

Same `.env` as DCA:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

If unset → no alerts (bot still trades). On start without Telegram you may see:  
`Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)`.

### Alert icons & tags

| Event | Icon | Tag / text |
|-------|------|------------|
| Bot start / adopt | 🤖 | FIB micro-grid started / `#FIB ADOPT` |
| Grid armed | 🧱 | `#FIB Grid armed` |
| OPEN / FILL | 🍏 LONG · 🍎 SHORT | `#FIB OPEN` / `#FIB FILL` |
| Protect trail | 🏄 | `#FIB TRAIL` |
| Disarm / error | ⚠️ | `#FIB DISARM` / `FIB error` |
| Closed | 🥳 / 😢 / 🤖 | `#FIB CLOSED` (by PnL) |

### Remote control

Requires `telegram_botctl.py` running **on the same machine** as the bot (pidfile + `pgrep`).

```bash
# Local foreground daemon
python3 telegram_botctl.py

# Or systemd (VPS)
sudo systemctl enable --now dca-telegram-ctl
sudo systemctl restart dca-telegram-ctl   # after pulling /fib support
```

| Command | Action |
|---------|--------|
| `/fib SYMBOL` | Start FIB in background |
| `/fib SYMBOL short` | Fixed side (`long` / `short` / `auto`) |
| `/stop SYMBOL` | Stop **DCA and/or FIB** (position & orders stay) |
| `/status SYMBOL` | Process + position summary |
| `/list` | All running bots (DCA + `FIB:SYMBOL`) |
| `/start SYMBOL` | Start **DCA** supervisor (unchanged) |
| `/cleanup SYMBOL` | Cancel `obstage*` algos (DCA staged) |
| `/help` | Command list |

### CLI equivalents (`botctl.py`)

```bash
python3 botctl.py fib LTCUSDT
python3 botctl.py fib LTCUSDT short
python3 botctl.py stop LTCUSDT          # DCA + FIB
python3 botctl.py fib-stop LTCUSDT      # FIB only
python3 botctl.py status LTCUSDT
python3 botctl.py list
```

---

## Environment variables

```text
# Core
OB_MG_GRID_MODE=fib
OB_MG_FIB_INTERVAL=5m
OB_MG_FIB_MIN_RANGE=0.40
OB_MG_ARM_MAX_FIB=0.236
OB_MG_FVG_MIN_PCT=0.08
OB_MG_REQUIRE_FVG=0
OB_MG_WAIT_PULLBACK=1
OB_MG_ARM_TIMEOUT_SEC=900
OB_MG_RAISE_TOP=1
OB_MG_RAISE_MIN_PCT=0.05

# Exits / protect / cooldown
OB_MG_TP_MODE=avg
OB_MG_TP_PCT=0.30
OB_MG_TP_FEE_PCT=0.08
OB_MG_SL_PCT=0.50
OB_MG_PROTECT_TRAIL=1
OB_MG_PROTECT_TRAIL_CALLBACK=0.2
OB_MG_PROTECT_ARM_PNL_PCT=0
OB_MG_COOLDOWN_SEC=3600

# Size
OB_MG_BASE_SIZE=10
OB_MG_LEVEL_SIZE=8
OB_MG_ENTRY_USDT=0
OB_MG_SET_LEVERAGE=0
OB_MG_NO_MAX_LEVERAGE=0
OB_MG_LEVELS=4
OB_MG_BAR_SEC=15
OB_MG_SWEEP=0

# Shared with DCA alerts / remote
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## Files & IDs

| Kind | Prefix / path |
|------|----------------|
| Grid LIMITs | `obmgG…` |
| Market entry | `obmgE…` |
| TP / SL / trail algos | `obmgTP` / `obmgSL` / `obmgTR` + symbol |
| Pidfile | `.run/pids/fib-SYMBOL.pid` |
| Journal | `.run/logs/SYMBOL/micro_grid.log` |
| `/fib` stdout | `.run/logs/fib-SYMBOL.log` |

---

## vs DCA (`./dca`)

| | FIB (`fib`) | DCA (`dca`) |
|--|-------------|-------------|
| Style | Fib pullback micro-grid | Wall / DCA grid + staged/trail exits |
| Default TF | 5m Fib + 15s OB | Supervisor loop |
| Protect | Full-fill trailing (default) | Exit plugins (`staged` / `trailing`) |
| Telegram start | `/fib SYMBOL` | `/start SYMBOL` |
| Telegram stop | `/stop SYMBOL` (both) | `/stop SYMBOL` |
| PM2 | No | Optional systemd `dca-futures@` |

---

## Other repo wrappers (scalp pool)

```bash
./obscalp-trades -f
./obscalp-pick
./obscalp-follow
./obscalp-watch
./obscalp-autotune

pm2 start|stop|restart scalp-pick
pm2 start|stop|restart scalp-follow
```

FIB micro-grid is **not** under PM2 by default — use `fib`, `/fib`, or `botctl.py fib`.
