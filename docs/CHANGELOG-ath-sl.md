# Changelog — ATH stop-loss (replaces risk-reduce partial)

**Date:** 2026-08-08  
**Scope:** Futures SHORT / pump→stall (`exits/risk_reduce.py`, DCA arm, auto-trade)

## Summary

The old **risk-reduce** addon cut part of the position above an HTF swing (tag `RR`) and kept a far full SL (tag `RF`). That partial reduction is **removed**.

There is now a **single** risk stop:

- Full-size `STOP_MARKET` (tag `RF`) at **historical ATH + 2%**
- New SHORT opens are **blocked** if price is **within 12%** of that ATH

Exit-mode SLs (staged BE, ratchet, protect-be, etc.) are unchanged; this only replaces the risk-reduce addon.

## Behaviour

| Rule | Default | Env / CLI |
|------|---------|-----------|
| Master switch | on | `RISK_REDUCE=1` / `--risk-reduce` / `--no-risk-reduce` |
| SL trigger | ATH × 1.02 | `RISK_ATH_SL_PCT=2` / `--risk-ath-sl-pct` |
| Entry block | gap to ATH &lt; 12% | `RISK_ATH_ENTRY_MIN_GAP_PCT=12` / `--risk-ath-entry-min-gap-pct` |

**ATH** = max daily high over paginated Binance USDT-M `1d` history (up to ~6000 bars).

**Entry gate** applies when:

1. Arming a **new** grid (`build_and_place_grid`, not `dca_only` re-arms)
2. Pumpstall **auto-trade** before launching `dca SYMBOL short --once`

Example: ATH = 100 → SL at 102; opens allowed only if last ≤ 88 (at least 12% below ATH).

## Removed / ignored

- Partial cut (`RR`, `RISK_REDUCE_PCT`, recovery floor for structure/BE after RR)
- Prior-swing / %% fallback full SL (`RISK_FULL_BUFFER_PCT`, `RISK_FULL_SWING_*`, `RISK_REDUCE_SWING_BARS`)
- Legacy CLI flags still parse (suppressed) so old unit lines do not crash; they are ignored

Any leftover `obstageRR*` algos are cancelled on the next risk sync.

## Files

- `exits/risk_reduce.py` — ATH SL + entry gate helpers
- `orderbook_dca_grid.py` — CLI + entry gate on new SHORT arms
- `pump_stall_scan.py` — auto-trade skip near ATH; Help snapshot fields
- `telegram_notify.py` / `pumpstall_telegram.py` — ATH SL shown on IDEAL card (🛡️ line); no separate `#SL` arm alert
- `.env.example` — new env vars
- Pumpstall site: `app/Support/StackParams.php`, `resources/views/help.blade.php`

## Deploy notes (VPS)

1. Pull / sync code to `/opt/trade-binance-websocket-orderbook-dca-grid`
2. Optional in `.env`:

   ```bash
   RISK_REDUCE=1
   RISK_ATH_SL_PCT=2
   RISK_ATH_ENTRY_MIN_GAP_PCT=12
   ```

3. Restart supervisors / `pump-stall-watch` (or `pump-stall-watch-early`) so children load the new logic
4. Open positions: next exit poll places/resyncs the ATH SL and drops any old `RR` partial

## Rollback

Set `RISK_REDUCE=0` (or `--no-risk-reduce`) to disable the ATH SL and the entry gate. Restoring the old partial-cut behaviour requires reverting this change in git.
