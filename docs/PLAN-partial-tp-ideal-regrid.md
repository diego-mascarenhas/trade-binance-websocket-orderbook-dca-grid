# Plan — After partial TP/SL, wait for ★ IDEAL to re-grid

**Date:** 2026-08-19  
**Status:** proposal (evaluate in office, then implement)  
**Context:** pump→stall shorts (`--exit ratchet` + `--partial-tp`), runner after 70% TP

---

## Goal

When a **partial TP** (or a future **partial SL**) fills:

1. Take the cut (70% TP today).
2. **Cancel management SL** (ratchet / BE / trail / ATH `RF`) so the runner is not stopped out on the squeeze.
3. **Do not** place a new DCA grid immediately.
4. Leave the runner open and the slot “attachable”.
5. If the scanner shows **★ IDEAL again** (rising edge: left ★, came back), **then** place one new DCA-only grid on that position.

Mental model: the 70% is the harvest. The 30% is a cheap option. More size only when the setup is ★ again — not because price moved, not because the process restarted.

---

## Why it failed in production (2026-08-18/19)

| Symbol | What we saw | Why no new grid |
|--------|-------------|-----------------|
| **PRLUSDT** | SL still on chart after deploy | `FUTURES_PAIRS` unset → `sync_pairs.py` skipped futures. Pump-stall children use `start_new_session=True`, so restarting `pump-stall-watch-early` did **not** reload `dca --once`. Old process kept the SL. Later: **no `dca` at all** → nobody cancels SL or re-grids. |
| **GPUSDT** | User thought no SL | Ticker is **GPSUSDT**. `dca` was alive from 02:56 with new flags. |
| **BTWUSDT** | “Didn’t open a grid again” | Log shows TP **armed**, not filled (`TAKE_PROFIT @ 0.40406`). PnL **−3.4%**. Ratchet waits for **+1%** before SL. Position still counted as occupied slot. If/when `dca` dies, pump-stall **will not relaunch** on a symbol that already has a position. |

Deploy log that matters:

```
FUTURES_PAIRS not set — skipping futures.
+ sudo systemctl restart pump-stall-watch-early
```

That restarts the **watcher**, not the `dca SYMBOL --once` children.

---

## Current behaviour vs desired

```
TODAY (after commit 301ec4b)
  TP1 fill
    → cancel SL
    → place DCA grid immediately
    → freeze management SL for the rest of the position
    → pump-stall: open position = occupied → never spawn a new dca
    → ★ re-arm pulse only if a dca supervisor is already running

DESIRED
  TP1 fill (and later: partial SL, if we add one)
    → cancel SL
    → cancel leftover DCA (stale walls)
    → do NOT place grid now
    → keep runner; keep dca --once alive if possible
    → on ★ rising edge → one DCA-only grid
    → if dca died but position remains: ★ IDEAL may re-attach
      (`dca SYMBOL short --once --force`) instead of treating the
      slot as a dead occupant
```

### ★ re-arm that already exists

`star_rearm.py` + `pump_stall_scan.sync_from_scan`:

- Watcher writes a token when a **supervised** symbol becomes ★ again.
- Supervisor consumes it on a successful DCA-only place (bypasses 12× cap once).
- First sight while already ★ (deploy) = inherit, **no** pulse.
- **Gap:** `running` = live `dca` PIDs only. Orphan position → no pulse, no attach.

---

## Proposed design

### A. Partial TP fill (`exits/partial_tp.py`)

On fill (qty shrink / mark already through / −2021 market cut):

| Step | Do |
|------|----|
| 1 | Cancel BE / ratchet / trail / leftover TP1 / ATH `RF` |
| 2 | Set `partial_tp_filled=True`, `sl_paused_after_partial=True` |
| 3 | Cancel leftover DCA limits |
| 4 | **Do not** call `build_and_place_grid` |
| 5 | Telegram: TP1 filled · SL off · waiting ★ for re-grid |

Keep the skip in `ratchet.py` / `be.py` / `risk_reduce.py` while `sl_paused_after_partial` (or `partial_tp_filled`) so SL is not re-armed on the next poll.

Keep `partial_tp_filled` so we do **not** arm a second 70% TP on the runner (burst gate would re-arm immediately while still ≥2% green).

### B. New grid only on ★ rising edge

Supervisor loop (`orderbook_dca_grid.py` --supervise):

- After partial TP, **do not** treat “position + 0 DCA” as auto re-arm (today: `Position open, no DCA grid → DCA-only re-arm`).
- Re-arm DCA-only when `star_rearm.pending(symbol)` **or** a new explicit flag `regrid_on_star_after_partial`.
- On successful place: `star_rearm.consume` + `mark_grid_active` (same as 12× bypass).

Watcher already pulses ★ if the symbol is in `running`. After this change, `running` for pulse purposes should include **open futures positions** on pump-stall pairs (or at least symbols with `partial_tp_filled`), not only live PIDs.

### C. Re-attach if `dca` died

Pump-stall `_occupied_trade_slots` today:

```
occupied = children ∪ live --supervise PIDs ∪ Binance open positions
```

Open position **blocks** a new launch. That is correct for “don’t open a 4th ★”, but wrong for “BTW still has 30% and no bot”.

Split the set:

| Set | Meaning |
|-----|---------|
| `hard_occupied` | Live `dca` (child or systemd) — real slot |
| `orphans` | Binance position **without** a live `dca` |

Policy:

- `MAX_TRADES` counts `hard_occupied` **plus** orphans (capital is still in use — don’t open a 4th coin).
- If an **orphan** is ★ IDEAL again → launch  
  `dca SYMBOL short --exit ratchet --no-protect-be --partial-tp --once --force`  
  so it attaches to the existing position (DCA-only, no new entry market).
- Do **not** launch a full new grid from flat on an orphan.
- Weekend / margin-soft gates still apply to this re-attach (same as new ★), unless we decide re-attach is maintenance (prefer: **same gates**; capital is already at risk).

`--force` already exists for “symbol already has a position”. Confirm `build_and_place_grid(..., dca_only=True, force=True)` is what `--force` + open position does.

### D. Partial SL (optional, phase 2)

Today there is **no** partial SL (ATH `RF` is full size; old `RR` cut was removed 2026-08-08).

If we want symmetry later:

- Define a partial SL (e.g. 30–50% at ratchet/ATH buffer).
- On fill: same as TP1 — SL off (or leave a far ATH only — **decide in office**), no immediate grid, wait for ★.

**Decision needed:** after partial TP, keep far ATH `RF` as catastrophe stop, or strip all stops (current 301ec4b strips ATH too). Recommendation for this plan: **strip management SL, keep ATH `RF`** so a blow-through still exits. Re-grid on ★ does not require being naked.

### E. Deploy / process lifetime (ops, not just code)

- Pump-stall `Popen(..., start_new_session=True)` survives `systemctl restart pump-stall-watch-early`. After a code pull, **old `dca --once` keep old logic**.
- Add to deploy docs / `deploy.sh`: list and restart (or kill+let watcher re-attach) live `orderbook_dca_grid.py SYMBOL --once` children, **or** stop using new session so they die with the watcher.
- `FUTURES_PAIRS` empty is OK for pump-stall-only VPS, but then deploy must not imply futures supervisors refreshed.

---

## Files to touch

| File | Change |
|------|--------|
| `exits/partial_tp.py` | Stop immediate `build_and_place_grid` on fill; keep SL pause + cancel DCA |
| `orderbook_dca_grid.py` | After `partial_tp_filled`, skip naked “no DCA → re-arm”; only re-arm if `star_rearm.pending` |
| `star_rearm.py` | Pulse ★ for orphan open positions (or `partial_tp_filled` symbols), not only live PIDs |
| `pump_stall_scan.py` | Orphan re-attach on ★; don’t treat orphan as “already covered, skip launch” |
| `telegram_notify.py` | Copy: SL off · waiting ★ (not “new grid”) |
| `pump-stall-watch-early` comment | Align with wait-for-★ |
| `deploy/deploy.sh` or `docs/` | Restart `--once` children after pull |
| Tests if any | Partial fill does not place grid; pending token does |

---

## Edge cases

- **★ the whole time after TP1:** no rising edge → no grid. Correct (same as inherit-on-deploy). Runner sits until ★ drops and returns, or we add a manual `/rearm`.
- **12× cap:** leftover 30% of 5× ≈ 1.5×; new grid usually fits. If still over cap, only ★ token bypasses (existing).
- **`--once`:** stay in supervise until **flat**, not until TP1. Confirm `--once` does not exit after partial fill.
- **Two grids:** cancel old DCA on TP1 so ★ places a clean book, not stacked leftovers.
- **Manual SL in the app:** `cancel_foreign_sl` only sees **algo** orders (`/fapi/v1/openAlgoOrders`). Chart STOP may be a normal order — out of scope unless we extend cancel.
- **Weekend block:** orphan re-attach during Fri 21:00–Sun 23:00 UTC — follow existing auto-trade gate.

---

## Acceptance checks

1. Dry-run / paper: arm TP1, fill (or mark-through) → logs `SL off · waiting ★`; **zero** new LIMIT DCA.
2. Force a ★ rising edge (leave then re-enter `ideal_near`) with `dca` alive → one DCA-only grid; telegram re-arm.
3. Kill `dca --once`, leave position, become ★ again → watcher starts `dca … --force`; grid appears; `MAX_TRADES` still 3 coins.
4. GPSUSDT-style: `dca` alive, TP1 not filled, red PnL → no new grid (nothing to do).
5. Deploy: after `git pull`, running `--once` processes are on the new code (or documented kill list).

---

## Out of scope

- Changing 70% / +0.3% / 5× gates  
- Bringing back risk-reduce `RR` partial cut (unless we explicitly add “partial SL” in phase 2)  
- Spot grid  
- `FUTURES_PAIRS` systemd fleet (pump-stall VPS may stay watcher-only)

---

## Office decisions (checkboxes)

- [ ] Immediate grid on TP1: **remove** (this plan) vs keep as fallback if ★ already true
- [ ] After TP1 keep **ATH `RF`** vs naked runner
- [ ] Orphan re-attach counts toward `MAX_TRADES` (yes in this plan)
- [ ] Kill `--once` children on watcher restart vs `start_new_session` + explicit restart in `deploy.sh`
- [ ] Phase 2 partial SL: yes/no
