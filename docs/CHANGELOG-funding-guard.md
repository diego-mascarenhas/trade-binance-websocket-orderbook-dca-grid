# Changelog — Funding fee guard

**Date:** 2026-08-15  
**Scope:** Futures opens + supervise flatten (`exits/funding.py`)

## Summary

The bot now reacts to Binance USD-M **funding** (separate from trading `TP_FEE_BUFFER`):

1. **Block new opens** when this side would *pay* funding ≥ threshold this window  
2. **Market-close** open positions in the lead window before `nextFundingTime` when we would pay ≥ threshold  

Who pays: rate &gt; 0 → LONGs pay; rate &lt; 0 → SHORTs pay (ACE-style).

## Defaults

| Env / CLI | Default | Meaning |
|-----------|---------|---------|
| `FUNDING_GUARD` / `--funding-guard` | on | Master switch |
| `FUNDING_PAY_MAX_PCT` / `--funding-pay-max-pct` | `0.3` | Pay %% that triggers block/close |
| `FUNDING_CLOSE_LEAD_MIN` / `--funding-close-lead-min` | `10` | Minutes before settlement to flatten |

Example: SHORT + funding rate −0.92% → pay 0.92% ≥ 0.3% → no new ★ / close ~10m before funding.

## Hooks

- `build_and_place_grid` — skip new arms (not `dca_only` re-arms)
- `pump_stall_scan` auto-trade — skip ★ launches
- `exits.run_exit_once` — flatten before primary exit (works with `--exit none` too)
- Close reason on Telegram: `funding · pay X% in Ym …`

## Files

- `exits/funding.py` — premiumIndex fetch + gates + close
- `exits/__init__.py` — run funding guard first in `run_exit_once`
- `orderbook_dca_grid.py` — entry gate + CLI + close-reason pop
- `pump_stall_scan.py` — auto-trade funding gate
- `.env.example` — new vars

## Occupied slots (2026-08-15)

`MAX_TRADES` now counts **all** of:

1. This watcher's `--once` children  
2. Any live `orderbook_dca_grid.py --supervise` process  
3. Open Binance USD-M positions  

So after a `pump-stall-watch` restart, orphans like ACEUSDT still consume a slot and the bot will not open past the cap.

