# Micro-grid (`./obmicro-grid`) — cheat sheet

Wrapper: `./obmicro-grid` → `orderbook_micro_grid.py`  
Journal: `.run/logs/SYMBOL/micro_grid.log`  
Stop: `Ctrl+C` (does **not** flatten; use `--flatten` to close)

---

## Quick start

```bash
./obmicro-grid LDOUSDT
./obmicro-grid LDOUSDT --dry-run
./obmicro-grid LDOUSDT --direction long
./obmicro-grid LDOUSDT --direction short
./obmicro-grid LDOUSDT --entry-usdt 50
./obmicro-grid SKLUSDT --direction short --no-wait-pullback --arm-max-fib 1
./obmicro-grid LDOUSDT --flatten
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
| `--flatten` | off | Cancel grid/exits + market-close position, then exit |

### Fib / grid

| Flag | Default | Description |
|------|---------|-------------|
| `--grid-mode` | `fib` | `fib` \| `step` |
| `--fib-interval` | `1m` | Swing TF (e.g. `5m`) |
| `--fib-lookback` | `40` | Lookback bars |
| `--fib-min-range` | `0.40` | Min swing range (%) |
| `--fib-max-span` | `12` | Max bars in impulse |
| `--fib-tp-ext` | `1.272` | TP extension (legacy) |
| `--fib-sl-buf` | `0.15` | % buffer beyond origin for SL |
| `--arm-max-fib` | `0.236` | Arm only while depth ≤ this Fib (window 0→max) |
| `--levels` | `4` | Number of LIMIT rungs |
| `--step-pct` | `0.08` | Only for `--grid-mode step` |

### Pullback / arming

| Flag | Default | Description |
|------|---------|-------------|
| `--wait-pullback` / `--no-wait-pullback` | on | LIMIT-only vs immediate MARKET |
| `--raise-top` / `--no-raise-top` | on | Raise Fib/TP if impulse extends (while pending) |
| `--raise-min-pct` | `0.05` | Min % extension to raise |
| `--arm-timeout-sec` | `900` | Disarm if no fill |
| `--require-fvg` / `--no-require-fvg` | off | Hard-require aligned FVG |
| `--fvg-min-pct` | `0.08` | Min FVG height (%) |

### Size / exits

| Flag | Default | Description |
|------|---------|-------------|
| `--entry-usdt` | `0` | Entry notional USDT (sets base-size; max leverage by default) |
| `--base-size` | `10` | First rung notional USDT (overridden by `--entry-usdt`) |
| `--level-size` | `8` | Deeper rung notional USDT |
| `--set-leverage` | `0` | Force leverage (`0` = symbol max) |
| `--no-max-leverage` | off | Do not raise to symbol max |
| `--tp-mode` | `avg` | `avg` (1st fill @ swing max, then avg±tp%) \| `swing` |
| `--tp-pct` | `0.35` | TP % from avg (avg mode) |
| `--sl-pct` | `0.50` | SL % from entry/mark |
| `--protect-trail` / `--no-protect-trail` | on | After all `--levels` fill + in profit → SL becomes trailing |
| `--protect-trail-callback` | `0.2` | Trailing `callbackRate` % (also min profit before arm) |
| `--protect-arm-pnl-pct` | `0` | Extra min mark profit % (effective min = max of this and callback) |
| `--sweep` / `--no-sweep` | off | Re-place filled rung further (barrido) |

### 15s signal (only with `--direction auto`)

| Flag | Default | Description |
|------|---------|-------------|
| `--bar-sec` | `15` | OB bar length |
| `--sample-sec` | `1` | Book sample interval |
| `--imb-long` | `0.55` | Min imbalance for long |
| `--imb-short` | `0.45` | Max imbalance for short |
| `--momentum-min-pct` | `0.01` | Min bar momentum |
| `--cooldown-sec` | `30` | Wait after flat/cycle |
| `--band-pct` | `1.0` | Depth band |
| `--depth-limit` | `50` | Depth levels |

---

## Optional environment variables

```text
OB_MG_GRID_MODE=fib
OB_MG_FIB_INTERVAL=1m
OB_MG_FIB_MIN_RANGE=0.40
OB_MG_ARM_MAX_FIB=0.236
OB_MG_FVG_MIN_PCT=0.08
OB_MG_REQUIRE_FVG=0
OB_MG_WAIT_PULLBACK=1
OB_MG_ARM_TIMEOUT_SEC=900
OB_MG_RAISE_TOP=1
OB_MG_RAISE_MIN_PCT=0.05
OB_MG_TP_MODE=avg
OB_MG_TP_PCT=0.35
OB_MG_SL_PCT=0.50
OB_MG_PROTECT_TRAIL=1
OB_MG_PROTECT_TRAIL_CALLBACK=0.2
OB_MG_BASE_SIZE=10
OB_MG_LEVEL_SIZE=8
OB_MG_ENTRY_USDT=0
OB_MG_SET_LEVERAGE=0
OB_MG_NO_MAX_LEVERAGE=0
OB_MG_LEVELS=4
OB_MG_BAR_SEC=15
OB_MG_SWEEP=0
```

---

## Useful recipes

**Paper (no orders)**  
```bash
./obmicro-grid LDOUSDT --dry-run
```

**Fixed long, Fib 5m**  
```bash
./obmicro-grid LDOUSDT --direction long --fib-interval 5m
```

**Direct entry (MARKET, no 0–0.236 window)**  
```bash
./obmicro-grid SKLUSDT --direction short --no-wait-pullback --arm-max-fib 1
```

**Hedge: short while LONG stays open**  
(account in hedge mode; bot only blocks the same side)  
```bash
./obmicro-grid SKLUSDT --direction short --position-mode hedge
```

**Close everything on symbol (grid + exits + position)**  
```bash
./obmicro-grid LDOUSDT --flatten
```

**Entry notional at max leverage**  
```bash
./obmicro-grid LDOUSDT --entry-usdt 50
# base=50U, level≈40U; sets symbol max leverage before arm
# margin ≈ 50 / leverage
./obmicro-grid LDOUSDT --entry-usdt 50 --set-leverage 20
./obmicro-grid LDOUSDT --entry-usdt 50 --no-max-leverage
```

**`Skip — existing LONG/SHORT` / adopt**  
If the same side is already open, the bot **adopts** it and rebuilds the live Fib table (no spam). It reuses open TP/SL algos when present, or arms them if missing.

```bash
# Restart while LONG is open → adopts + table
./obmicro-grid LDOUSDT

# Force-close instead:
./obmicro-grid LDOUSDT --flatten

# Hedge: open the opposite side while LONG stays open
./obmicro-grid LDOUSDT --direction short --position-mode hedge
```

---

## Behavior (summary)

1. 15s OB bar → signal (`auto`) or fixed side (`--direction`).
2. Fib: arm only if mark is between Fib **0.000** and **`--arm-max-fib`** (default 0.236).
3. Default: LIMIT pullback (no MARKET); exchange TP/SL after first fill.
4. `--no-wait-pullback`: MARKET base + grid + TP/SL immediately.
5. When all `--levels` are filled and mark profit ≥ callback % → replace SL with **trailing** (default on; TP kept).
6. Hedge: allows inverse side if the same side is flat.
7. Same-side position already open → **adopt** + rebuild live table (not spam skip).
8. Ctrl+C does not flatten; `--flatten` does.
9. `--entry-usdt N`: notional size; leverage set to symbol max unless `--no-max-leverage` / `--set-leverage`.

---

## Other repo wrappers (scalp pool)

```bash
./obscalp-trades -f          # live trades feed
./obscalp-pick               # picker
./obscalp-follow             # follow
./obscalp-watch              # watch
./obscalp-autotune           # autotune
./obscalp-sounds             # sounds

pm2 start|stop|restart scalp-pick
pm2 start|stop|restart scalp-follow
```

Micro-grid is **not** under PM2 by default; run it manually with `./obmicro-grid`.
