#!/usr/bin/env python3
"""On-demand capital-reduce (separate from the DCA supervisor).

Default (STOP ladder):
  · Walls = open obdca* prices; STOP size = DCA_notional / --comp-mult
  · Only arm when EMA trend is *against* the position
  · Flat/favorable: cancel our orders, leave supervisor DCA

--structure (aggressive micro when adverse):
  · Pre-arm REDUCE/DCA LIMITs at nearby OB + 1m liquidity swings (don't wait for touch)
  · LONG: reduce above / DCA below · SHORT: inverse
  · Break → cancel that wall; wick reject → market reduce once
  · --ws: Futures bookTicker + kline_1m WebSocket — live ticker; APPROVED/EXECUTE on wick
    (REST still used to place orders / refresh depth)

Examples:
  ./capital-reduce XRPUSDT --dry-run
  ./capital-reduce XRPUSDT --structure --ws
  ./capital-reduce XRPUSDT --structure --watch --interval 20
  ./capital-reduce XRPUSDT --ignore-trend
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orderbook_dca_grid as grid  # noqa: E402


TAG_REDUCE = "obcrR"  # capital-reduce STOP_MARKET algos / REDUCE LIMITs
TAG_COMP = "obcrC"  # capital-reduce compensate / structure DCA LIMITs
STATE_BLOCK_DCA = "capital_reduce_block_dca"
STATE_ACTIVE = "capital_reduce_active"
FSTREAM_BASE = os.getenv("BINANCE_FSTREAM", "wss://fstream.binance.com")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def allow_dca_rearm(symbol: str) -> bool:
    """False while a capital-reduce ladder owns the adverse walls."""
    try:
        import orderbook_staged_exit as staged

        st = staged.load_state(symbol.upper())
        return not bool(st.get(STATE_BLOCK_DCA))
    except Exception:
        return True


def set_dca_block(symbol: str, blocked: bool) -> None:
    import orderbook_staged_exit as staged

    sym = symbol.upper()
    st = staged.load_state(sym)
    if blocked:
        st[STATE_BLOCK_DCA] = True
        st[STATE_ACTIVE] = True
    else:
        st.pop(STATE_BLOCK_DCA, None)
        st.pop(STATE_ACTIVE, None)
    staged.save_state(sym, st)


def reduce_client_id(symbol: str, idx: int) -> str:
    # Binance rejects reuse of the same newClientOrderId while it is still
    # remembered / open — suffix a short time token so each place is unique.
    tok = int(time.time() * 1000) % 1_000_000
    return f"{TAG_REDUCE}{symbol.upper()}{idx:02d}{tok:06d}"


def comp_client_id(symbol: str, idx: int) -> str:
    tok = int(time.time() * 1000) % 1_000_000
    return f"{TAG_COMP}{symbol.upper()}{idx:02d}{tok:06d}"


def assess_adverse_trend(
    symbol: str,
    is_long: bool,
    *,
    interval: str = "15m",
    fast: int = 7,
    slow: int = 25,
    slope_min_pct: float = 0.05,
) -> tuple[bool, str, Any]:
    """Return (adverse, label, ema_snap).

    LONG is adverse on bearish EMA; SHORT on bullish. Flat / unknown → not adverse
    (keep DCA only, no STOP reduce).
    """
    from ob_ema import fetch_ema_snapshot

    snap = fetch_ema_snapshot(
        symbol,
        interval=interval,
        fast=fast,
        slow=slow,
        slope_min_pct=slope_min_pct,
    )
    if snap is None:
        return False, "unknown (no EMA)", None
    side = "LONG" if is_long else "SHORT"
    if is_long:
        adverse = snap.trend == "bearish"
        need = "bearish"
    else:
        adverse = snap.trend == "bullish"
        need = "bullish"
    label = (
        f"EMA{fast}/{slow} {interval} trend={snap.trend} slope={snap.slope_pct:+.3f}% "
        f"· {side} needs {need} → {'ADVERSE → arm STOPs' if adverse else 'not adverse → DCA only'}"
    )
    return adverse, label, snap


def _pnl_on_close(is_long: bool, entry: float, fill: float, qty: float) -> float:
    if entry <= 0 or qty <= 0:
        return 0.0
    if is_long:
        return (fill - entry) * qty
    return (entry - fill) * qty


def _new_avg(
    is_long: bool,
    qty: float,
    avg: float,
    *,
    close_qty: float = 0.0,
    add_qty: float = 0.0,
    add_px: float = 0.0,
) -> tuple[float, float]:
    """Apply close then add; return (qty, avg). Closing does not change avg."""
    q = max(0.0, qty - close_qty)
    a = avg
    if add_qty > 0 and add_px > 0:
        if q <= 0:
            return add_qty, add_px
        notional = q * a + add_qty * add_px
        q = q + add_qty
        a = notional / q
    return q, a


def fetch_dca_walls(
    symbol: str,
    is_long: bool,
    mark: float,
    api: str,
    sec: str,
    recv: int,
) -> list[tuple[float, float, float, float]]:
    """Open obdca* limits as walls: (price, dca_qty, dist_pct, dca_usdt)."""
    oo = grid._signed_request(
        "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
    ) or []
    out: list[tuple[float, float, float, float]] = []
    for o in oo if isinstance(oo, list) else []:
        cid = grid._order_client_id(o)
        if not cid.startswith("obdca"):
            continue
        if cid.startswith("obdcaE"):
            continue
        try:
            px = float(o.get("price") or 0)
            qty = float(o.get("origQty") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or qty <= 0:
            continue
        if is_long and px >= mark * 0.9999:
            continue
        if (not is_long) and px <= mark * 1.0001:
            continue
        dist = abs(px - mark) / mark * 100 if mark > 0 else 0.0
        out.append((px, qty, dist, qty * px))
    out.sort(key=lambda t: (-t[0] if is_long else t[0]))
    return out


def build_plan(
    *,
    symbol: str,
    is_long: bool,
    qty: float,
    entry: float,
    mark: float,
    lev: float,
    walls: list[tuple[float, float, float]],
    reduce_pct: float,
    comp_mult: float,
    now_trim_pct: float,
    filt: dict[str, Decimal],
    dca_walls: list[tuple[float, float, float, float]] | None = None,
) -> dict[str, Any]:
    """If dca_walls set: STOP = dca_usdt / comp_mult at each DCA price."""
    step = filt["step_size"]
    min_qty = float(filt["min_qty"])
    min_notional = float(filt["min_notional"])
    mult = max(1.0, float(comp_mult))

    steps: list[dict[str, Any]] = []
    run_qty = float(qty)
    run_avg = float(entry)
    realized = 0.0
    trim_qty = 0.0
    source = "dca" if dca_walls else "ob"

    if now_trim_pct > 0 and run_qty > 0:
        raw = run_qty * (now_trim_pct / 100.0)
        tq = float(grid._round_to(raw, step, ROUND_DOWN))
        if tq >= min_qty and tq * mark >= min_notional:
            pnl = _pnl_on_close(is_long, run_avg, mark, tq)
            realized += pnl
            run_qty, run_avg = _new_avg(is_long, run_qty, run_avg, close_qty=tq)
            trim_qty = tq
            steps.append(
                {
                    "kind": "NOW_TRIM",
                    "idx": 0,
                    "wall_px": mark,
                    "wall_qty": None,
                    "dist_pct": 0.0,
                    "reduce_qty": tq,
                    "reduce_usdt": tq * mark,
                    "comp_qty": 0.0,
                    "comp_usdt": 0.0,
                    "dca_qty": 0.0,
                    "dca_usdt": 0.0,
                    "realized_pnl": pnl,
                    "qty_after": run_qty,
                    "avg_after": run_avg,
                    "notional_after": run_qty * mark,
                    "margin_after": (run_qty * mark) / lev if lev else 0.0,
                }
            )

    if dca_walls:
        for i, (wpx, dca_q, dist, dca_u) in enumerate(dca_walls, start=1):
            if run_qty < min_qty:
                break
            red_usdt = dca_u / mult
            raw_red = red_usdt / wpx if wpx > 0 else 0.0
            red_q = float(grid._round_to(raw_red, step, ROUND_DOWN))
            if red_q < min_qty or red_q * wpx < min_notional:
                continue
            if run_qty - red_q < min_qty:
                red_q = float(grid._round_to(run_qty - min_qty, step, ROUND_DOWN))
                if red_q < min_qty:
                    break
            red_usdt = red_q * wpx
            pnl = _pnl_on_close(is_long, run_avg, wpx, red_q)
            realized += pnl
            run_qty, run_avg = _new_avg(is_long, run_qty, run_avg, close_qty=red_q)
            steps.append(
                {
                    "kind": "WALL",
                    "idx": i,
                    "wall_px": wpx,
                    "wall_qty": dca_q,
                    "dist_pct": dist,
                    "reduce_qty": red_q,
                    "reduce_usdt": red_usdt,
                    "comp_qty": dca_q,
                    "comp_usdt": dca_u,
                    "dca_qty": dca_q,
                    "dca_usdt": dca_u,
                    "realized_pnl": pnl,
                    "qty_after": run_qty,
                    "avg_after": run_avg,
                    "notional_after": run_qty * (mark if mark > 0 else wpx),
                    "margin_after": (run_qty * mark) / lev if lev and mark else 0.0,
                }
            )
    else:
        for i, (wpx, wqty, dist) in enumerate(walls, start=1):
            if run_qty < min_qty:
                break
            raw_red = run_qty * (reduce_pct / 100.0)
            red_q = float(grid._round_to(raw_red, step, ROUND_DOWN))
            if red_q < min_qty or red_q * wpx < min_notional:
                continue
            if run_qty - red_q < min_qty:
                red_q = float(grid._round_to(run_qty - min_qty, step, ROUND_DOWN))
                if red_q < min_qty:
                    break
            red_usdt = red_q * wpx
            comp_usdt = red_usdt * mult
            raw_comp = comp_usdt / wpx
            comp_q = float(grid._round_to(raw_comp, step, ROUND_DOWN))
            if comp_q < min_qty or comp_q * wpx < min_notional:
                comp_q = 0.0
                comp_usdt = 0.0
            else:
                comp_usdt = comp_q * wpx
            pnl = _pnl_on_close(is_long, run_avg, wpx, red_q)
            realized += pnl
            run_qty, run_avg = _new_avg(
                is_long, run_qty, run_avg, close_qty=red_q, add_qty=comp_q, add_px=wpx,
            )
            steps.append(
                {
                    "kind": "WALL",
                    "idx": i,
                    "wall_px": wpx,
                    "wall_qty": wqty,
                    "dist_pct": dist,
                    "reduce_qty": red_q,
                    "reduce_usdt": red_usdt,
                    "comp_qty": comp_q,
                    "comp_usdt": comp_usdt,
                    "dca_qty": 0.0,
                    "dca_usdt": 0.0,
                    "realized_pnl": pnl,
                    "qty_after": run_qty,
                    "avg_after": run_avg,
                    "notional_after": run_qty * (mark if mark > 0 else wpx),
                    "margin_after": (run_qty * mark) / lev if lev and mark else 0.0,
                }
            )

    start_notional = qty * (mark if mark > 0 else entry)
    end_notional = run_qty * (mark if mark > 0 else entry)
    n_walls = len(dca_walls) if dca_walls else len(walls)
    return {
        "symbol": symbol.upper(),
        "is_long": is_long,
        "side": "LONG" if is_long else "SHORT",
        "source": source,
        "qty0": qty,
        "entry0": entry,
        "mark": mark,
        "lev": lev,
        "notional0": start_notional,
        "margin0": start_notional / lev if lev else 0.0,
        "qty_end": run_qty,
        "avg_end": run_avg,
        "notional_end": end_notional,
        "margin_end": end_notional / lev if lev else 0.0,
        "realized_pnl": realized,
        "trim_qty": trim_qty,
        "steps": steps,
        "walls_found": n_walls,
        "comp_mult": mult,
    }


def render_plan(
    plan: dict[str, Any],
    *,
    reduce_pct: float,
    comp_mult: float,
    with_comp: bool = False,
) -> str:
    side = plan["side"]
    is_long = plan["is_long"]
    dir_color = grid.GREEN if is_long else grid.RED
    mark = plan["mark"]
    entry = plan["entry0"]
    adverse = ((entry - mark) / entry * 100) if is_long else ((mark - entry) / entry * 100)
    source = plan.get("source", "ob")
    mult = float(plan.get("comp_mult") or comp_mult)

    lines: list[str] = []
    if with_comp:
        mode = "STOP reduce + COMP LIMIT"
    elif source == "dca":
        mode = f"STOP off DCA prices (DCA ≈ ×{mult:g} STOP)"
    else:
        mode = "STOP reduce only (OB walls fallback)"
    lines.append(
        f"{grid.BOLD}{grid.CYAN}Capital reduce · {plan['symbol']} · "
        f"{dir_color}{side}{grid.CYAN} · {mode}{grid.RESET}"
    )
    lines.append(
        f"{grid.DIM}entry {grid.price_fmt(entry)}  ·  mark {grid.price_fmt(mark)}  ·  "
        f"adverse {adverse:+.2f}%  ·  lev {plan['lev']:g}x  ·  "
        + (
            f"STOP = DCA/{mult:g}"
            if source == "dca" and not with_comp
            else f"reduce {reduce_pct:g}%/wall"
        )
        + (f"  ·  comp ×{mult:g}" if with_comp else "")
        + f"{grid.RESET}"
    )
    lines.append(
        f"{grid.DIM}open qty {grid.qty_fmt(plan['qty0'])}  ·  "
        f"notional {plan['notional0']:,.2f} USDT  ·  "
        f"margin {plan['margin0']:,.2f} USDT{grid.RESET}"
    )
    lines.append("")

    if source == "dca" and not with_comp:
        header = (
            f"{'#':>3} {'WALL':>12} {'ΔMARK':>7} "
            f"{'STOP qty':>10} {'−USDT':>8} "
            f"{'DCA qty':>10} {'+USDT':>8} "
            f"{'DCA/STOP':>8} {'qty→':>10}"
        )
    elif with_comp:
        header = (
            f"{'#':>3} {'WALL':>12} {'ΔMARK':>7} "
            f"{'REDUCE qty':>12} {'−USDT':>9} "
            f"{'COMP qty':>12} {'+USDT':>9} "
            f"{'rPnL':>9} {'qty→':>12}"
        )
    else:
        header = (
            f"{'#':>3} {'WALL':>12} {'ΔMARK':>7} "
            f"{'REDUCE qty':>12} {'−USDT':>9} "
            f"{'rPnL':>9} {'qty→':>12}"
        )
    lines.append(f"{grid.DIM}{header}{grid.RESET}")
    lines.append(f"{grid.DIM}{'─' * len(header)}{grid.RESET}")

    run_qty = float(plan["qty0"])
    for s in plan["steps"]:
        label = "NOW" if s["kind"] == "NOW_TRIM" else f"{s['idx']}"
        dmark = (s["wall_px"] / mark - 1.0) * 100 if mark > 0 else 0.0
        red = float(s["reduce_qty"])
        run_qty = max(0.0, run_qty - red)
        if source == "dca" and not with_comp:
            dca_u = float(s.get("dca_usdt") or s.get("comp_usdt") or 0)
            dca_q = float(s.get("dca_qty") or s.get("comp_qty") or 0)
            ratio = (dca_u / s["reduce_usdt"]) if s["reduce_usdt"] else 0.0
            lines.append(
                f"{label:>3} {grid.price_fmt(s['wall_px']):>12} {dmark:>+6.2f}% "
                f"{grid.qty_fmt(red):>10} {s['reduce_usdt']:>8.2f} "
                f"{grid.qty_fmt(dca_q):>10} {dca_u:>8.2f} "
                f"{ratio:>7.2f}x {grid.qty_fmt(run_qty):>10}"
            )
        elif with_comp:
            rp = s["realized_pnl"]
            rp_c = grid.GREEN if rp >= 0 else grid.RED
            lines.append(
                f"{label:>3} {grid.price_fmt(s['wall_px']):>12} {dmark:>+6.2f}% "
                f"{grid.qty_fmt(s['reduce_qty']):>12} {s['reduce_usdt']:>9.2f} "
                f"{grid.qty_fmt(s['comp_qty']):>12} {s['comp_usdt']:>9.2f} "
                f"{rp_c}{rp:>+9.2f}{grid.RESET} "
                f"{grid.qty_fmt(s['qty_after']):>12}"
            )
        else:
            rp = s["realized_pnl"]
            rp_c = grid.GREEN if rp >= 0 else grid.RED
            lines.append(
                f"{label:>3} {grid.price_fmt(s['wall_px']):>12} {dmark:>+6.2f}% "
                f"{grid.qty_fmt(red):>12} {s['reduce_usdt']:>9.2f} "
                f"{rp_c}{rp:>+9.2f}{grid.RESET} "
                f"{grid.qty_fmt(run_qty):>12}"
            )

    if not plan["steps"]:
        lines.append(
            f"{grid.YELLOW}No walls / steps sized — need open obdca* or loosen OB filters."
            f"{grid.RESET}"
        )

    lines.append("")
    total_red = sum(float(s["reduce_qty"]) for s in plan["steps"] if s["kind"] == "WALL")
    total_dca = sum(float(s.get("dca_usdt") or 0) for s in plan["steps"] if s["kind"] == "WALL")
    total_stop_u = sum(float(s["reduce_usdt"]) for s in plan["steps"] if s["kind"] == "WALL")
    n = sum(1 for s in plan["steps"] if s["kind"] == "WALL")
    if source == "dca" and not with_comp:
        lines.append(
            f"{grid.BOLD}STOP ladder off DCA{grid.RESET}  {n} walls  ·  "
            f"STOP −{total_stop_u:,.2f} USDT  ·  DCA +{total_dca:,.2f} USDT  ·  "
            f"closes up to {grid.qty_fmt(total_red)}"
        )
        lines.append(
            f"{grid.DIM}At each DCA price: STOP closes DCA/{mult:g}; supervisor DCA keeps the add "
            f"(~{mult:g}× reduce).{grid.RESET}"
        )
    elif with_comp:
        lines.append(
            f"{grid.BOLD}After full ladder{grid.RESET}  qty → {grid.qty_fmt(plan['qty_end'])}  ·  "
            f"avg → {grid.price_fmt(plan['avg_end'])}"
        )
        lines.append(
            f"{grid.DIM}--with-comp: cancels obdca* and blocks supervisor re-arm.{grid.RESET}"
        )
    else:
        lines.append(
            f"{grid.BOLD}STOP ladder{grid.RESET}  closes up to {grid.qty_fmt(total_red)} "
            f"across {n} OB walls ({reduce_pct:g}% of remaining each)."
        )
    return "\n".join(lines)


def fetch_walls_for_position(
    symbol: str,
    is_long: bool,
    mark: float,
    args: argparse.Namespace,
) -> list[tuple[float, float, float]]:
    """Walls in the adverse direction from *mark* (continue the pain path)."""
    depth = grid.fetch_depth(symbol, args.limit)
    bids = [[float(p), float(q)] for p, q in (depth.get("bids") or [])]
    asks = [[float(p), float(q)] for p, q in (depth.get("asks") or [])]
    # LONG underwater → need BID walls below; SHORT → ASK walls above
    levels = bids if is_long else asks
    return grid.select_walls(
        levels,
        mark,
        is_long,
        args.so_count,
        args.min_gap,
        args.min_dist,
        args.max_range,
    )


def fetch_book_walls(
    symbol: str,
    mark: float,
    args: argparse.Namespace,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Nearest significant supports (bids below) and resistances (asks above)."""
    depth = grid.fetch_depth(symbol, args.limit)
    bids = [[float(p), float(q)] for p, q in (depth.get("bids") or [])]
    asks = [[float(p), float(q)] for p, q in (depth.get("asks") or [])]
    n = max(1, int(getattr(args, "struct_count", 2) or 2))
    # Nearer first rung so local liquidity wicks have a level to hit
    min_dist = min(float(args.min_dist), 0.05)
    supports = grid.select_walls(
        bids, mark, True, n, args.min_gap, min_dist, args.max_range,
    )
    resistances = grid.select_walls(
        asks, mark, False, n, args.min_gap, min_dist, args.max_range,
    )
    return supports, resistances


def fetch_liquidity_swings(
    symbol: str,
    mark: float,
    *,
    bars: int = 8,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Recent 1m high/low as liquidity levels (catches wicks OB may miss)."""
    from futures_scan import FAPI_BASE, fetch_klines

    kl = fetch_klines(FAPI_BASE, symbol.upper(), "1m", max(3, bars))
    if len(kl) < 2 or mark <= 0:
        return [], []
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    liq_hi = max(highs)
    liq_lo = min(lows)
    supports: list[tuple[float, float, float]] = []
    resistances: list[tuple[float, float, float]] = []
    if liq_lo < mark * 0.9999:
        supports.append((liq_lo, 0.0, (mark - liq_lo) / mark * 100))
    if liq_hi > mark * 1.0001:
        resistances.append((liq_hi, 0.0, (liq_hi - mark) / mark * 100))
    return supports, resistances


def _merge_wall_levels(
    primary: list[tuple[float, float, float]],
    extra: list[tuple[float, float, float]],
    *,
    mark: float,
    count: int,
    min_gap: float,
) -> list[tuple[float, float, float]]:
    """Merge OB + swing levels, nearest first, de-duped by min_gap."""
    if mark <= 0:
        return primary[:count]
    pooled = list(primary) + list(extra)
    pooled.sort(key=lambda t: t[2])  # nearest
    out: list[tuple[float, float, float]] = []
    for price, qty, dist in pooled:
        if any(abs(price - p) / mark * 100 < min_gap for p, _, _ in out):
            continue
        out.append((price, qty, dist))
        if len(out) >= count:
            break
    return out


def _wall_touch_hold(
    *,
    mark: float,
    wall_px: float,
    is_support: bool,
    touch_pct: float,
    break_pct: float,
) -> tuple[str, float]:
    """Classify mark vs wall.

    Returns (status, dist_pct) where status is:
      hold   — within touch band and not broken
      far    — not yet near (still OK to *pre-arm* resting LIMITs)
      broken — price pushed through the wall
    """
    if mark <= 0 or wall_px <= 0:
        return "far", 0.0
    dist_pct = abs(mark - wall_px) / wall_px * 100.0
    if is_support:
        if mark < wall_px * (1.0 - break_pct / 100.0):
            return "broken", dist_pct
        if mark <= wall_px * (1.0 + touch_pct / 100.0):
            return "hold", dist_pct
        return "far", dist_pct
    if mark > wall_px * (1.0 + break_pct / 100.0):
        return "broken", dist_pct
    if mark >= wall_px * (1.0 - touch_pct / 100.0):
        return "hold", dist_pct
    return "far", dist_pct


def _recent_1m_extremes(symbol: str) -> tuple[float, float, float]:
    """Return (high, low, close) over recent 1m bars including forming."""
    from futures_scan import FAPI_BASE, fetch_klines

    kl = fetch_klines(FAPI_BASE, symbol.upper(), "1m", 5)
    if not kl:
        return 0.0, 0.0, 0.0
    hi = max(float(k[2]) for k in kl)
    lo = min(float(k[3]) for k in kl)
    close = float(kl[-1][4])
    return hi, lo, close


def _already_chased_wick(symbol: str, level: float, tol_pct: float = 0.05) -> bool:
    import orderbook_staged_exit as staged

    st = staged.load_state(symbol.upper())
    prev = float(st.get("cr_wick_level") or 0)
    if prev <= 0 or level <= 0:
        return False
    return abs(prev - level) / level * 100 <= tol_pct


def _mark_wick_chased(symbol: str, level: float) -> None:
    import orderbook_staged_exit as staged

    sym = symbol.upper()
    st = staged.load_state(sym)
    st["cr_wick_level"] = float(level)
    st["cr_wick_ts"] = time.time()
    staged.save_state(sym, st)


def _dca_prices_near(
    symbol: str,
    api: str,
    sec: str,
    recv: int,
) -> list[float]:
    oo = grid._signed_request(
        "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
    ) or []
    out: list[float] = []
    for o in oo if isinstance(oo, list) else []:
        cid = grid._order_client_id(o)
        if not cid.startswith("obdca") or cid.startswith("obdcaE"):
            continue
        try:
            px = float(o.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            out.append(px)
    return out


def _overlaps_price(px: float, others: list[float], gap_pct: float) -> bool:
    if px <= 0:
        return False
    for o in others:
        if o <= 0:
            continue
        if abs(px - o) / px * 100.0 < gap_pct:
            return True
    return False


def build_structure_actions(
    *,
    symbol: str,
    is_long: bool,
    qty: float,
    entry: float,
    mark: float,
    supports: list[tuple[float, float, float]],
    resistances: list[tuple[float, float, float]],
    reduce_pct: float,
    comp_mult: float,
    touch_pct: float,
    break_pct: float,
    min_gap: float,
    existing_dca_px: list[float],
    filt: dict[str, Decimal],
    prearm: bool = True,
    live_hi: float | None = None,
    live_lo: float | None = None,
) -> list[dict[str, Any]]:
    """REDUCE/DCA at walls: pre-arm resting LIMITs; chase wick rejects with market.

    Waiting for mark to already be in the touch band misses liquidity grabs
    (wick up/down in seconds). Pre-arm places LIMITs ahead; wick_now catches
    a grab that already rejected. live_hi/lo come from the WS kline/mark feed.
    """
    step = filt["step_size"]
    min_qty = float(filt["min_qty"])
    min_notional = float(filt["min_notional"])
    mult = max(1.0, float(comp_mult))
    actions: list[dict[str, Any]] = []

    if is_long:
        reduce_walls = [("resistance", w) for w in resistances]
        dca_walls = [("support", w) for w in supports]
    else:
        reduce_walls = [("support", w) for w in supports]
        dca_walls = [("resistance", w) for w in resistances]

    # Wick chase: REST 1m extremes merged with live WS hi/lo
    hi, lo, _close = _recent_1m_extremes(symbol)
    if live_hi and live_hi > 0:
        hi = max(hi, float(live_hi))
    if live_lo and live_lo > 0:
        lo = min(lo, float(live_lo)) if lo > 0 else float(live_lo)
    wick_level = 0.0
    wick_ext = 0.0
    if is_long and hi > mark > 0:
        wick_ext = (hi - mark) / mark * 100
        # Rejected: high printed above mark, mark back below high - break band
        if wick_ext >= max(0.15, touch_pct * 0.5) and mark < hi * (1.0 - break_pct / 100.0):
            wick_level = hi
    elif (not is_long) and lo > 0 and lo < mark:
        wick_ext = (mark - lo) / mark * 100
        if wick_ext >= max(0.15, touch_pct * 0.5) and mark > lo * (1.0 + break_pct / 100.0):
            wick_level = lo

    run_qty = float(qty)
    idx = 0

    if wick_level > 0 and run_qty >= min_qty and not _already_chased_wick(symbol, wick_level):
        raw = run_qty * (reduce_pct / 100.0)
        rq = float(grid._round_to(raw, step, ROUND_DOWN))
        if rq >= min_qty and rq * mark >= min_notional:
            if run_qty - rq < min_qty:
                rq = float(grid._round_to(run_qty - min_qty, step, ROUND_DOWN))
            if rq >= min_qty:
                idx += 1
                actions.append({
                    "role": "REDUCE",
                    "status": "wick_now",
                    "idx": idx,
                    "wall_kind": "liquidity",
                    "wall_px": wick_level,
                    "wall_qty": 0.0,
                    "dist_pct": wick_ext,
                    "qty": rq,
                    "usdt": rq * mark,
                    "realized_pnl": _pnl_on_close(is_long, entry, mark, rq),
                })
                run_qty = max(0.0, run_qty - rq)

    for kind, (wpx, wqty, dist) in reduce_walls:
        is_sup = kind == "support"
        status, d_now = _wall_touch_hold(
            mark=mark, wall_px=wpx, is_support=is_sup,
            touch_pct=touch_pct, break_pct=break_pct,
        )
        idx += 1
        if status == "broken":
            actions.append({
                "role": "REDUCE", "status": "broken", "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        # Pre-arm resting LIMITs while far/hold — do not wait for touch
        can_arm = status in ("hold", "far") if prearm else status == "hold"
        if not can_arm or run_qty < min_qty:
            actions.append({
                "role": "REDUCE", "status": status, "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        raw = run_qty * (reduce_pct / 100.0)
        rq = float(grid._round_to(raw, step, ROUND_DOWN))
        if rq < min_qty or rq * wpx < min_notional:
            actions.append({
                "role": "REDUCE", "status": "too_small", "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        if run_qty - rq < min_qty:
            rq = float(grid._round_to(run_qty - min_qty, step, ROUND_DOWN))
            if rq < min_qty:
                continue
        actions.append({
            "role": "REDUCE", "status": "arm", "idx": idx,
            "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
            "dist_pct": d_now, "qty": rq, "usdt": rq * wpx,
            "book_status": status,
            "realized_pnl": _pnl_on_close(is_long, entry, wpx, rq),
        })
        run_qty = max(0.0, run_qty - rq)

    for kind, (wpx, wqty, dist) in dca_walls:
        is_sup = kind == "support"
        status, d_now = _wall_touch_hold(
            mark=mark, wall_px=wpx, is_support=is_sup,
            touch_pct=touch_pct, break_pct=break_pct,
        )
        idx += 1
        if status == "broken":
            actions.append({
                "role": "DCA", "status": "broken", "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        can_arm = status in ("hold", "far") if prearm else status == "hold"
        if not can_arm:
            actions.append({
                "role": "DCA", "status": status, "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        if _overlaps_price(wpx, existing_dca_px, min_gap):
            actions.append({
                "role": "DCA", "status": "skip_obdca", "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        base = float(qty) * (reduce_pct / 100.0) * mult
        raw_q = base / wpx if wpx > 0 else 0.0
        dq = float(grid._round_to(raw_q, step, ROUND_DOWN))
        if dq < min_qty or dq * wpx < min_notional:
            actions.append({
                "role": "DCA", "status": "too_small", "idx": idx,
                "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
                "dist_pct": d_now, "qty": 0.0, "usdt": 0.0,
            })
            continue
        actions.append({
            "role": "DCA", "status": "arm", "idx": idx,
            "wall_kind": kind, "wall_px": wpx, "wall_qty": wqty,
            "dist_pct": d_now, "qty": dq, "usdt": dq * wpx,
            "book_status": status,
        })
    return actions


def render_structure(
    *,
    symbol: str,
    is_long: bool,
    qty: float,
    entry: float,
    mark: float,
    lev: float,
    actions: list[dict[str, Any]],
    reduce_pct: float,
    comp_mult: float,
) -> str:
    side = "LONG" if is_long else "SHORT"
    dir_c = grid.GREEN if is_long else grid.RED
    adverse = ((entry - mark) / entry * 100) if is_long else ((mark - entry) / entry * 100)
    lines = [
        f"{grid.BOLD}{grid.CYAN}Structure · {symbol} · {dir_c}{side}{grid.CYAN} · "
        f"pre-arm LIMITs + wick chase{grid.RESET}",
        f"{grid.DIM}entry {grid.price_fmt(entry)}  ·  mark {grid.price_fmt(mark)}  ·  "
        f"adverse {adverse:+.2f}%  ·  lev {lev:g}x  ·  "
        f"reduce {reduce_pct:g}%  ·  DCA ×{comp_mult:g}{grid.RESET}",
        f"{grid.DIM}open qty {grid.qty_fmt(qty)}  ·  "
        f"notional {qty * mark:,.2f} USDT{grid.RESET}",
        "",
    ]
    header = (
        f"{'ROLE':<7} {'WALL':<11} {'PX':>12} {'ΔMARK':>7} "
        f"{'STATUS':<11} {'QTY':>10} {'USDT':>8}"
    )
    lines.append(f"{grid.DIM}{header}{grid.RESET}")
    lines.append(f"{grid.DIM}{'─' * len(header)}{grid.RESET}")
    for a in actions:
        dmark = (a["wall_px"] / mark - 1.0) * 100 if mark > 0 else 0.0
        st = a["status"]
        if st in ("arm", "wick_now"):
            st_c = grid.GREEN
        elif st == "broken":
            st_c = grid.RED
        elif st == "hold":
            st_c = grid.CYAN
        else:
            st_c = grid.DIM
        q = float(a.get("qty") or 0)
        u = float(a.get("usdt") or 0)
        lines.append(
            f"{a['role']:<7} {a['wall_kind']:<11} {grid.price_fmt(a['wall_px']):>12} "
            f"{dmark:>+6.2f}% {st_c}{st:<11}{grid.RESET} "
            f"{grid.qty_fmt(q) if q else '—':>10} {u if u else 0:>8.2f}"
        )
    armed_r = sum(1 for a in actions if a["role"] == "REDUCE" and a["status"] == "arm")
    wick_n = sum(1 for a in actions if a["status"] == "wick_now")
    armed_d = sum(1 for a in actions if a["role"] == "DCA" and a["status"] == "arm")
    broken = sum(1 for a in actions if a["status"] == "broken")
    lines.append("")
    lines.append(
        f"{grid.BOLD}Arm {armed_r} REDUCE + {armed_d} DCA"
        f"{f' + {wick_n} wick market' if wick_n else ''}{grid.RESET}"
        f"{grid.DIM}  ·  {broken} broken (skip)  ·  supervisor obdca* left alone{grid.RESET}"
    )
    return "\n".join(lines)


def list_existing_cr_orders(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    oo = grid._signed_request(
        "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
    ) or []
    out = []
    for o in oo if isinstance(oo, list) else []:
        cid = grid._order_client_id(o)
        if cid.startswith(TAG_REDUCE) or cid.startswith(TAG_COMP):
            out.append(o)
    return out


def cancel_cr_orders(symbol: str, api: str, sec: str, recv: int) -> int:
    killed = 0
    for o in list_existing_cr_orders(symbol, api, sec, recv):
        try:
            grid._signed_request(
                "DELETE",
                "/fapi/v1/order",
                {"symbol": symbol.upper(), "orderId": o.get("orderId")},
                api,
                sec,
                recv,
            )
            killed += 1
        except Exception as exc:
            print(f"{grid.RED}Cancel {grid._order_client_id(o)} failed: {exc}{grid.RESET}")
    if killed:
        print(f"{grid.YELLOW}Cancelled {killed} prior capital-reduce LIMIT(s).{grid.RESET}")
    return killed


def cancel_cr_algos(symbol: str, api: str, sec: str, recv: int) -> int:
    """Cancel STOP reduce algos tagged obcrR*."""
    import orderbook_staged_exit as staged

    killed = 0
    try:
        algos = staged.list_open_algo_orders(symbol, api, sec, recv)
    except Exception as exc:
        print(f"{grid.YELLOW}List algo orders failed: {exc}{grid.RESET}")
        return 0
    for o in algos:
        cid = staged._algo_client_id(o)
        if not cid.startswith(TAG_REDUCE):
            continue
        try:
            if staged.cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
                killed += 1
        except Exception as exc:
            print(f"{grid.RED}Cancel algo {cid} failed: {exc}{grid.RESET}")
    if killed:
        print(f"{grid.YELLOW}Cancelled {killed} prior capital-reduce STOP(s).{grid.RESET}")
    return killed


def _place_comp_limit(
    *,
    symbol: str,
    is_long_pos: bool,
    qty: float,
    price: float,
    hedge: bool,
    client_id: str,
    api: str,
    sec: str,
    recv: int,
    filt: dict[str, Decimal],
) -> dict:
    """Resting LIMIT that adds to the position at the wall."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    side = "BUY" if is_long_pos else "SELL"
    if side == "BUY":
        px = grid._round_to(price, tick, ROUND_DOWN)
    else:
        px = grid._round_to(price, tick, ROUND_UP)
    qd = grid._round_to(qty, step, ROUND_DOWN)
    if qd <= 0:
        raise RuntimeError("qty rounded to 0")
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{qd:.{qty_dp}f}",
        "price": f"{float(px):.{price_dp}f}",
        "newClientOrderId": client_id,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long_pos else "SHORT"
    return grid._signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def _place_reduce_limit(
    *,
    symbol: str,
    is_long: bool,
    qty: float,
    price: float,
    hedge: bool,
    client_id: str,
    api: str,
    sec: str,
    recv: int,
    filt: dict[str, Decimal],
) -> dict:
    """Resting LIMIT that reduces the position (sell into resistance / buy into support)."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    side = "SELL" if is_long else "BUY"
    # Maker-friendly: sell above / buy below
    if side == "SELL":
        px = grid._round_to(price, tick, ROUND_UP)
    else:
        px = grid._round_to(price, tick, ROUND_DOWN)
    qd = grid._round_to(qty, step, ROUND_DOWN)
    if qd <= 0:
        raise RuntimeError("qty rounded to 0")
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": f"{qd:.{qty_dp}f}",
        "price": f"{float(px):.{price_dp}f}",
        "newClientOrderId": client_id,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return grid._signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def apply_structure(
    *,
    symbol: str,
    is_long: bool,
    actions: list[dict[str, Any]],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    filt: dict[str, Decimal],
) -> int:
    """Sync REDUCE/DCA LIMITs + chase wick rejects with market (no full cancel churn)."""
    cancel_cr_algos(symbol, api, sec, recv)  # drop legacy STOP mode algos
    set_dca_block(symbol, False)
    tick = float(filt["tick_size"])
    tol = max(tick * 2, (actions[0]["wall_px"] if actions else 1.0) * 0.0002)

    desired_arm = [a for a in actions if a.get("status") == "arm" and float(a.get("qty") or 0) > 0]
    wick_acts = [a for a in actions if a.get("status") == "wick_now" and float(a.get("qty") or 0) > 0]
    broken_px = {
        float(a["wall_px"])
        for a in actions
        if a.get("status") == "broken" and float(a.get("wall_px") or 0) > 0
    }

    open_cr = list_existing_cr_orders(symbol, api, sec, recv)
    kept = 0
    cancelled = 0
    for o in open_cr:
        cid = grid._order_client_id(o)
        try:
            px = float(o.get("price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        role = "REDUCE" if cid.startswith(TAG_REDUCE) else "DCA"
        # Cancel if wall broken or no longer desired
        if any(abs(px - bp) <= tol for bp in broken_px):
            drop = True
        else:
            drop = not any(
                a["role"] == role and abs(float(a["wall_px"]) - px) <= tol
                for a in desired_arm
            )
        if drop:
            try:
                grid._signed_request(
                    "DELETE",
                    "/fapi/v1/order",
                    {"symbol": symbol.upper(), "orderId": o.get("orderId")},
                    api,
                    sec,
                    recv,
                )
                cancelled += 1
            except Exception as exc:
                print(f"{grid.RED}Cancel {cid} failed: {exc}{grid.RESET}")
        else:
            kept += 1
    if cancelled:
        print(f"{grid.YELLOW}Cancelled {cancelled} stale/broken structure LIMIT(s).{grid.RESET}")
    if kept:
        print(f"{grid.DIM}Kept {kept} resting structure LIMIT(s).{grid.RESET}")

    # Refresh open set after cancels
    open_cr = list_existing_cr_orders(symbol, api, sec, recv)

    def _have(role: str, wpx: float) -> bool:
        for o in open_cr:
            cid = grid._order_client_id(o)
            r = "REDUCE" if cid.startswith(TAG_REDUCE) else "DCA"
            if r != role:
                continue
            try:
                px = float(o.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if abs(px - wpx) <= tol:
                return True
        return False

    placed = 0
    for a in wick_acts:
        from orderbook_staged_exit import _market_reduce_qty

        qty = float(a["qty"])
        try:
            closed = _market_reduce_qty(
                symbol, is_long, Decimal(str(qty)), hedge, filt, api, sec, recv,
            )
            _mark_wick_chased(symbol, float(a["wall_px"]))
            placed += 1
            print(
                f"  {grid.GREEN}WICK REDUCE{grid.RESET} market "
                f"{grid.qty_fmt(closed)} · liq={grid.price_fmt(a['wall_px'])} "
                f"(ext {a['dist_pct']:+.2f}%)"
            )
        except Exception as exc:
            print(f"{grid.RED}Wick market reduce failed: {exc}{grid.RESET}")

    for a in desired_arm:
        idx = int(a["idx"])
        wpx = float(a["wall_px"])
        qty = float(a["qty"])
        role = a["role"]
        if _have(role, wpx):
            continue
        if role == "REDUCE":
            side = "SELL" if is_long else "BUY"
            cid = reduce_client_id(symbol, idx)
            try:
                _place_reduce_limit(
                    symbol=symbol,
                    is_long=is_long,
                    qty=qty,
                    price=wpx,
                    hedge=hedge,
                    client_id=cid,
                    api=api,
                    sec=sec,
                    recv=recv,
                    filt=filt,
                )
                placed += 1
                open_cr = list_existing_cr_orders(symbol, api, sec, recv)
                print(
                    f"  {grid.YELLOW}REDUCE LIMIT{grid.RESET} {side} "
                    f"{grid.qty_fmt(qty)} @ {grid.price_fmt(wpx)} "
                    f"({a['wall_kind']} pre-arm) ({cid})"
                )
            except Exception as exc:
                print(f"{grid.RED}Reduce LIMIT failed #{idx}: {exc}{grid.RESET}")
        elif role == "DCA":
            side = "BUY" if is_long else "SELL"
            cid = comp_client_id(symbol, idx)
            try:
                _place_comp_limit(
                    symbol=symbol,
                    is_long_pos=is_long,
                    qty=qty,
                    price=wpx,
                    hedge=hedge,
                    client_id=cid,
                    api=api,
                    sec=sec,
                    recv=recv,
                    filt=filt,
                )
                placed += 1
                open_cr = list_existing_cr_orders(symbol, api, sec, recv)
                print(
                    f"  {grid.CYAN}DCA   LIMIT{grid.RESET} {side} "
                    f"{grid.qty_fmt(qty)} @ {grid.price_fmt(wpx)} "
                    f"({a['wall_kind']} pre-arm) ({cid})"
                )
            except Exception as exc:
                print(f"{grid.RED}DCA LIMIT failed #{idx}: {exc}{grid.RESET}")

    print(
        f"{grid.BOLD}Structure sync on {symbol.upper()}: "
        f"+{placed} new · kept {kept} · cancelled {cancelled}{grid.RESET}"
    )
    return placed


def _place_reduce_stop(
    *,
    symbol: str,
    is_long: bool,
    qty: float,
    trigger: float,
    mark: float,
    hedge: bool,
    client_id: str,
    api: str,
    sec: str,
    recv: int,
    filt: dict[str, Decimal],
) -> dict:
    """STOP_MARKET reduce-only: fires when adverse price reaches the wall."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    # LONG close on the way down → round trigger down; SHORT close on the way up → up
    if is_long:
        trig = grid._round_to(trigger, tick, ROUND_DOWN)
    else:
        trig = grid._round_to(trigger, tick, ROUND_UP)
    qd = grid._round_to(qty, step, ROUND_DOWN)
    if qd <= 0:
        raise RuntimeError("qty rounded to 0")
    trig_f = float(trig)
    import orderbook_staged_exit as staged

    if staged._stop_would_immediately_trigger(is_long, trig_f, mark, tick):
        raise RuntimeError(
            f"STOP @ {grid.price_fmt(trig_f)} would trigger now (mark {grid.price_fmt(mark)})"
        )
    side = "SELL" if is_long else "BUY"
    params: dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": side,
        "type": "STOP_MARKET",
        "quantity": f"{qd:.{qty_dp}f}",
        "triggerPrice": f"{trig_f:.{price_dp}f}",
        "workingType": "CONTRACT_PRICE",
        "clientAlgoId": client_id,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return grid._signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def apply_plan(
    plan: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    sym = plan["symbol"]
    is_long = plan["is_long"]
    recv = args.recv_window
    mark = float(plan["mark"])
    with_comp = bool(getattr(args, "with_comp", False))

    # Always refresh our prior capital-reduce orders
    cancel_cr_algos(sym, api, sec, recv)
    cancel_cr_orders(sym, api, sec, recv)

    if with_comp:
        n_dca = grid.cancel_dca_grid_orders(sym, api, sec, recv)
        if n_dca:
            print(f"{grid.YELLOW}Cleared old DCA grid ({n_dca} obdca*) before arming.{grid.RESET}")
        set_dca_block(sym, True)
        print(
            f"{grid.CYAN}DCA re-arm blocked for {sym} (--with-comp). "
            f"Clear with --allow-dca.{grid.RESET}"
        )
    else:
        # Default: supervisor keeps DCA
        set_dca_block(sym, False)
        print(
            f"{grid.DIM}Leaving supervisor DCA (obdca*) in place — "
            f"only arming STOP reduces.{grid.RESET}"
        )

    # Optional immediate market trim
    for s in plan["steps"]:
        if s["kind"] != "NOW_TRIM":
            continue
        from orderbook_staged_exit import _market_reduce_qty

        closed = _market_reduce_qty(
            sym, is_long, Decimal(str(s["reduce_qty"])), hedge, filt, api, sec, recv,
        )
        print(
            f"{grid.GREEN}✓ Market trim {grid.qty_fmt(closed)} on {sym} "
            f"@ mark ~{grid.price_fmt(mark)}{grid.RESET}"
        )

    placed = 0
    for s in plan["steps"]:
        if s["kind"] != "WALL":
            continue
        idx = int(s["idx"])
        wpx = float(s["wall_px"])
        if s["reduce_qty"] > 0:
            red_side = "SELL" if is_long else "BUY"
            cid = reduce_client_id(sym, idx)
            try:
                _place_reduce_stop(
                    symbol=sym,
                    is_long=is_long,
                    qty=float(s["reduce_qty"]),
                    trigger=wpx,
                    mark=mark,
                    hedge=hedge,
                    client_id=cid,
                    api=api,
                    sec=sec,
                    recv=recv,
                    filt=filt,
                )
                placed += 1
                print(
                    f"  {grid.YELLOW}REDUCE STOP{grid.RESET} {red_side} "
                    f"{grid.qty_fmt(s['reduce_qty'])} trigger={grid.price_fmt(wpx)} "
                    f"({cid})"
                )
            except Exception as exc:
                print(f"{grid.RED}Reduce STOP failed wall#{idx}: {exc}{grid.RESET}")
        if with_comp and s["comp_qty"] > 0:
            add_side = "BUY" if is_long else "SELL"
            cid = comp_client_id(sym, idx)
            try:
                _place_comp_limit(
                    symbol=sym,
                    is_long_pos=is_long,
                    qty=float(s["comp_qty"]),
                    price=wpx,
                    hedge=hedge,
                    client_id=cid,
                    api=api,
                    sec=sec,
                    recv=recv,
                    filt=filt,
                )
                placed += 1
                print(
                    f"  {grid.CYAN}COMP  LIMIT{grid.RESET} {add_side} "
                    f"{grid.qty_fmt(s['comp_qty'])} @ {grid.price_fmt(wpx)} "
                    f"({cid})"
                )
            except Exception as exc:
                print(f"{grid.RED}Comp place failed wall#{idx}: {exc}{grid.RESET}")

    kind = "STOP reduce + LIMIT comp" if with_comp else "STOP reduce only"
    print(f"{grid.BOLD}Placed {placed} capital-reduce order(s) on {sym} ({kind}).{grid.RESET}")


def run_symbol(symbol: str, args: argparse.Namespace) -> int:
    api, sec = grid.load_keys(args.env_file)
    if not api or not sec:
        print(f"{grid.RED}Missing BINANCE_API_KEY / BINANCE_SECRET_KEY{grid.RESET}")
        return 2
    recv = args.recv_window

    if getattr(args, "allow_dca", False):
        set_dca_block(symbol, False)
        print(f"{grid.GREEN}✓ DCA re-arm allowed again for {symbol.upper()}{grid.RESET}")
        return 0

    hedge = False
    try:
        resp = grid._signed_request("GET", "/fapi/v1/positionSide/dual", {}, api, sec, recv)
        hedge = bool(resp.get("dualSidePosition"))
    except Exception as exc:
        print(f"{grid.YELLOW}position mode detect failed ({exc}); assuming hedge={hedge}{grid.RESET}")

    side_is_long, qty, entry = grid._detect_open_side(symbol, hedge, api, sec, recv)
    if side_is_long is None or qty <= 0 or entry <= 0:
        print(f"{grid.YELLOW}{symbol}: no open position — skip{grid.RESET}")
        return 1

    meta = grid.get_position_meta(symbol, side_is_long, hedge, api, sec, recv)
    mark = float(meta.get("mark") or 0) or float(grid._live_mid(symbol) or entry)
    live_mark = getattr(args, "_live_mark", None)
    if live_mark and float(live_mark) > 0:
        mark = float(live_mark)
    lev = float(meta.get("leverage") or 0) or float(
        grid.get_symbol_leverage(symbol, api, sec, recv)
    )

    # Trend gate: only reduce when price is moving against the position
    ignore_trend = bool(getattr(args, "ignore_trend", False))
    adverse = True
    trend_label = "ignored (--ignore-trend)"
    if not ignore_trend:
        adverse, trend_label, _snap = assess_adverse_trend(
            symbol,
            side_is_long,
            interval=str(getattr(args, "trend_interval", "15m") or "15m"),
            fast=int(getattr(args, "trend_ema_fast", 7) or 7),
            slow=int(getattr(args, "trend_ema_slow", 25) or 25),
            slope_min_pct=float(getattr(args, "trend_slope_min", 0.05) or 0.05),
        )
    side = "LONG" if side_is_long else "SHORT"
    print(f"{grid.CYAN}Trend · {symbol.upper()} {side} · {trend_label}{grid.RESET}")

    if not adverse:
        # Cancel any prior reduce STOPs; leave supervisor DCA untouched
        if args.dry_run:
            print(
                f"{grid.DIM}Dry-run: would cancel obcrR*/obcrC* and leave DCA only "
                f"(trend not against us).{grid.RESET}"
            )
            return 0
        n_stop = cancel_cr_algos(symbol, api, sec, recv)
        n_comp = cancel_cr_orders(symbol, api, sec, recv)
        set_dca_block(symbol, False)
        print(
            f"{grid.GREEN}✓ Trend not adverse → DCA only "
            f"(cancelled {n_stop} STOP + {n_comp} CR LIMITs). No new reduces.{grid.RESET}"
        )
        return 0

    filt = grid.load_symbol_filters(symbol)

    # ── Structure mode: pre-arm LIMITs + wick chase when adverse ──
    if bool(getattr(args, "structure", False)):
        supports, resistances = fetch_book_walls(symbol, mark, args)
        sw_sup, sw_res = fetch_liquidity_swings(symbol, mark, bars=8)
        n = max(1, int(args.struct_count))
        supports = _merge_wall_levels(
            supports, sw_sup, mark=mark, count=n, min_gap=float(args.min_gap),
        )
        resistances = _merge_wall_levels(
            resistances, sw_res, mark=mark, count=n, min_gap=float(args.min_gap),
        )
        existing_dca = _dca_prices_near(symbol, api, sec, recv)
        actions = build_structure_actions(
            symbol=symbol,
            is_long=side_is_long,
            qty=float(qty),
            entry=float(entry),
            mark=mark,
            supports=supports,
            resistances=resistances,
            reduce_pct=args.reduce_pct,
            comp_mult=args.comp_mult,
            touch_pct=float(args.touch_pct),
            break_pct=float(args.break_pct),
            min_gap=float(args.min_gap),
            existing_dca_px=existing_dca,
            filt=filt,
            prearm=True,
            live_hi=getattr(args, "_live_hi", None),
            live_lo=getattr(args, "_live_lo", None),
        )
        print(render_structure(
            symbol=symbol.upper(),
            is_long=side_is_long,
            qty=float(qty),
            entry=float(entry),
            mark=mark,
            lev=lev,
            actions=actions,
            reduce_pct=args.reduce_pct,
            comp_mult=args.comp_mult,
        ))
        hi, lo, _ = _recent_1m_extremes(symbol)
        print(
            f"{grid.DIM}Book+swing: {len(supports)} support(s) · {len(resistances)} resistance(s)  ·  "
            f"1m hi/lo {grid.price_fmt(hi)}/{grid.price_fmt(lo)}  ·  "
            f"touch≤{args.touch_pct:g}%  break>{args.break_pct:g}%  ·  "
            f"obdca*{len(existing_dca)}{grid.RESET}"
        )
        if args.dry_run:
            print(
                f"\n{grid.DIM}Dry-run only. Omit --dry-run to sync structure "
                f"LIMITs / wick market.{grid.RESET}"
            )
            return 0
        print(f"\n{grid.BOLD}{grid.YELLOW}EXECUTE · structure (adverse)…{grid.RESET}")
        apply_structure(
            symbol=symbol,
            is_long=side_is_long,
            actions=actions,
            hedge=hedge,
            api=api,
            sec=sec,
            recv=recv,
            filt=filt,
        )
        return 0

    dca_walls = fetch_dca_walls(symbol, side_is_long, mark, api, sec, recv)
    walls: list[tuple[float, float, float]] = []
    use_dca = bool(dca_walls) and not bool(getattr(args, "with_comp", False))
    if use_dca:
        print(
            f"{grid.DIM}Using {len(dca_walls)} open obdca* prices as STOP triggers "
            f"(STOP = DCA / {args.comp_mult:g}).{grid.RESET}"
        )
    else:
        if not dca_walls and not getattr(args, "with_comp", False):
            print(
                f"{grid.YELLOW}No open obdca* — falling back to order-book walls "
                f"+ --reduce-pct.{grid.RESET}"
            )
        walls = fetch_walls_for_position(symbol, side_is_long, mark, args)
    plan = build_plan(
        symbol=symbol,
        is_long=side_is_long,
        qty=float(qty),
        entry=float(entry),
        mark=mark,
        lev=lev,
        walls=walls,
        reduce_pct=args.reduce_pct,
        comp_mult=args.comp_mult,
        now_trim_pct=args.now_trim_pct,
        filt=filt,
        dca_walls=dca_walls if use_dca else None,
    )
    print(render_plan(
        plan,
        reduce_pct=args.reduce_pct,
        comp_mult=args.comp_mult,
        with_comp=bool(getattr(args, "with_comp", False)),
    ))

    # Show open bot DCA / prior CR for context
    try:
        import orderbook_staged_exit as staged

        oo = grid._signed_request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
        ) or []
        dca = [o for o in oo if grid._order_client_id(o).startswith("obdca")]
        cr = [o for o in oo if grid._order_client_id(o).startswith("obcr")]
        algos = staged.list_open_algo_orders(symbol, api, sec, recv)
        cr_stop = [a for a in algos if staged._algo_client_id(a).startswith(TAG_REDUCE)]
        print(
            f"{grid.DIM}Open now: {len(dca)} obdca*  ·  {len(cr)} obcrC LIMIT  ·  "
            f"{len(cr_stop)} obcrR STOP  ·  walls picked {plan['walls_found']}/{args.so_count}"
            f"{grid.RESET}"
        )
    except Exception:
        pass

    if args.dry_run:
        print(
            f"\n{grid.DIM}Dry-run only. Omit --dry-run to place STOP reduces"
            f"{' + COMP' if getattr(args, 'with_comp', False) else ''}."
            f"{grid.RESET}"
        )
        return 0

    mode = "STOP reduce + COMP" if getattr(args, "with_comp", False) else "STOP reduce only"
    print(f"\n{grid.BOLD}{grid.YELLOW}EXECUTE · {mode} (trend adverse)…{grid.RESET}")
    apply_plan(plan, args, hedge, api, sec, filt)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    grid.load_env_file(None)
    p = argparse.ArgumentParser(
        description="STOP-reduce ladder on adverse walls; DCA stays with supervisor by default",
    )
    p.add_argument("symbols", nargs="+", help="e.g. PUMPUSDT XRPUSDT")
    p.add_argument(
        "--reduce-pct",
        type=float,
        default=_env_float("CAPITAL_REDUCE_PCT", 5.0),
        help="%% of *remaining* position qty to close at each wall (default 5). "
             "Env: CAPITAL_REDUCE_PCT",
    )
    p.add_argument(
        "--comp-mult",
        type=float,
        default=_env_float("CAPITAL_COMP_MULT", 1.25),
        help="Default: STOP notional = DCA_notional / this (DCA ≈ ×1.25 of reduce). "
             "Also used with --with-comp. Env: CAPITAL_COMP_MULT",
    )
    p.add_argument(
        "--with-comp",
        action="store_true",
        help="Also place COMP LIMITs and take over DCA (cancel obdca* + block re-arm)",
    )
    p.add_argument(
        "--trend-interval",
        default=os.getenv("CAPITAL_TREND_INTERVAL", "15m") or "15m",
        help="EMA timeframe for adverse-trend gate (default 15m). "
             "Env: CAPITAL_TREND_INTERVAL",
    )
    p.add_argument("--trend-ema-fast", type=int, default=_env_int("CAPITAL_TREND_EMA_FAST", 7))
    p.add_argument("--trend-ema-slow", type=int, default=_env_int("CAPITAL_TREND_EMA_SLOW", 25))
    p.add_argument(
        "--trend-slope-min",
        type=float,
        default=_env_float("CAPITAL_TREND_SLOPE_MIN", 0.05),
        help="Min |EMA slope| %% to count as bullish/bearish (default 0.05)",
    )
    p.add_argument(
        "--ignore-trend",
        action="store_true",
        help="Arm STOPs/structure even if trend is not against the position",
    )
    p.add_argument(
        "--structure",
        action="store_true",
        help="When adverse: REDUCE LIMIT on resistance hold (LONG) / support hold (SHORT); "
             "DCA LIMIT on support hold (LONG) / resistance hold (SHORT). Skip broken walls. "
             "Leaves supervisor obdca* alone.",
    )
    p.add_argument(
        "--struct-count",
        type=int,
        default=_env_int("CAPITAL_STRUCT_COUNT", 2),
        help="Max support + max resistance walls to evaluate (default 2). "
             "Env: CAPITAL_STRUCT_COUNT",
    )
    p.add_argument(
        "--touch-pct",
        type=float,
        default=_env_float("CAPITAL_TOUCH_PCT", 0.45),
        help="%% from wall to count as touch/hold (default 0.45). Env: CAPITAL_TOUCH_PCT",
    )
    p.add_argument(
        "--break-pct",
        type=float,
        default=_env_float("CAPITAL_BREAK_PCT", 0.15),
        help="%% through wall = broken, do not arm (default 0.15). Env: CAPITAL_BREAK_PCT",
    )
    p.add_argument(
        "--now-trim-pct",
        type=float,
        default=_env_float("CAPITAL_NOW_TRIM_PCT", 0.0),
        help="Optional immediate market reduce %% of open qty before the wall ladder "
             "(default 0). Env: CAPITAL_NOW_TRIM_PCT",
    )
    p.add_argument("--so-count", type=int, default=_env_int("CAPITAL_SO_COUNT", 6),
                   help="Adverse walls to plan (default 6)")
    p.add_argument("--limit", type=int, default=1000, help="OB depth")
    p.add_argument("--min-gap", type=float, default=_env_float("CAPITAL_MIN_GAP", 0.8))
    p.add_argument("--min-dist", type=float, default=0.15)
    p.add_argument("--max-range", type=float, default=_env_float("CAPITAL_MAX_RANGE", 15.0))
    p.add_argument("--recv-window", type=int, default=_env_int("RECV_WINDOW", 15000))
    p.add_argument("--env-file", default=None)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only — do not place/cancel orders",
    )
    p.add_argument(
        "--execute",
        "--apply",
        dest="execute",
        action="store_true",
        help="Explicit live execution (already the default if --dry-run is omitted)",
    )
    p.add_argument(
        "--allow-dca",
        action="store_true",
        help="Clear capital-reduce DCA block (after a prior --with-comp run)",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Keep running: re-check trend + sync every --interval seconds (Ctrl+C to stop)",
    )
    p.add_argument(
        "--ws",
        action="store_true",
        help="WebSocket watch (fstream bookTicker + kline_1m): live ticker + wick APPROVE/EXECUTE. "
             "Implies continuous run. Best with --structure.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=_env_float("CAPITAL_WATCH_INTERVAL", 60.0),
        help="Seconds between full REST sync in --watch/--ws (default 60). "
             "Env: CAPITAL_WATCH_INTERVAL",
    )
    args = p.parse_args(argv)
    args.reduce_pct = max(0.5, min(40.0, float(args.reduce_pct)))
    args.comp_mult = max(1.0, min(3.0, float(args.comp_mult)))
    args.now_trim_pct = max(0.0, min(50.0, float(args.now_trim_pct)))
    args.interval = max(10.0 if getattr(args, "structure", False) else 15.0, float(args.interval))
    args.struct_count = max(1, min(6, int(args.struct_count)))
    args.touch_pct = max(0.05, min(2.0, float(args.touch_pct)))
    args.break_pct = max(0.05, min(2.0, float(args.break_pct)))
    if args.ws and not args.structure:
        print(
            f"{grid.YELLOW}Note: --ws is most useful with --structure "
            f"(wick/touch reduce). Continuing anyway.{grid.RESET}"
        )
    return args


def _run_once_all(args: argparse.Namespace) -> int:
    rc = 0
    for i, sym in enumerate(args.symbols):
        if i:
            print()
        code = run_symbol(sym.upper(), args)
        rc = rc or code
    return rc


def _ws_stream_url(symbols: list[str]) -> str:
    # bookTicker is the reliable live feed here (markPrice@1s often stalls).
    parts: list[str] = []
    for sym in symbols:
        s = sym.lower()
        parts.append(f"{s}@bookTicker")
        parts.append(f"{s}@kline_1m")
    return f"{FSTREAM_BASE.rstrip('/')}/stream?streams={'/'.join(parts)}"


def run_ws_watch(args: argparse.Namespace) -> int:
    """Futures WS loop: instant wick/touch reacts + periodic full REST sync."""
    try:
        import websocket  # websocket-client
    except ImportError:
        print(
            f"{grid.RED}websocket-client not installed. "
            f"Run: .venv/bin/pip install websocket-client{grid.RESET}"
        )
        return 2

    symbols = [s.upper() for s in args.symbols]
    url = _ws_stream_url(symbols)
    interval = float(args.interval)
    touch_pct = float(args.touch_pct)
    min_wick = max(0.12, touch_pct * 0.4)
    break_pct = float(args.break_pct)

    state: dict[str, Any] = {
        "mark": {s: 0.0 for s in symbols},
        "hi": {s: 0.0 for s in symbols},
        "lo": {s: 0.0 for s in symbols},
        "px_hist": {s: [] for s in symbols},  # [(ts, px), ...] rolling window
        "is_long": {s: None for s in symbols},  # True LONG / False SHORT / None unknown
        "ticks": {s: 0 for s in symbols},
        "klines": {s: 0 for s in symbols},
        "lock": threading.Lock(),
        "busy": False,
        "stop": False,
        "last_full": 0.0,
        "last_react": {s: 0.0 for s in symbols},
        "last_paint": 0.0,
        "cycle": 0,
        "last_event": "boot",
        "status": "listening",
    }
    paint_lock = threading.Lock()
    # Rolling window for hi/lo — session min forever made stale lo look like a live wick
    extreme_window_sec = float(getattr(args, "wick_window", 90.0) or 90.0)

    print(
        f"{grid.BOLD}{grid.CYAN}capital-reduce --ws"
        f"{' --structure' if args.structure else ''}{grid.RESET}  "
        f"{', '.join(symbols)}  ·  full sync every {interval:g}s  ·  Ctrl+C to stop"
    )
    print(f"{grid.DIM}{url}{grid.RESET}")
    print(
        f"{grid.DIM}Live ticker on one line · wick≥{min_wick:.2f}% triggers APPROVE/EXECUTE"
        f"{grid.RESET}"
    )
    if args.dry_run:
        print(f"{grid.YELLOW}(--dry-run: will not place/cancel orders){grid.RESET}")

    # Initial REST sync before stream
    print(f"\n{grid.DIM}── initial REST sync ──{grid.RESET}")
    try:
        _run_once_all(args)
    except Exception as exc:
        print(f"{grid.RED}Initial sync error: {exc}{grid.RESET}")
    state["last_full"] = time.time()
    # Cache position side so wick direction is correct (LONG→reduce on ↑ only)
    try:
        api, sec = grid.load_keys(args.env_file)
        recv = args.recv_window
        hedge = False
        try:
            resp = grid._signed_request(
                "GET", "/fapi/v1/positionSide/dual", {}, api, sec, recv,
            )
            hedge = bool(resp.get("dualSidePosition"))
        except Exception:
            pass
        for sym in symbols:
            side_is_long, qty, _entry = grid._detect_open_side(sym, hedge, api, sec, recv)
            if side_is_long is not None and qty > 0:
                state["is_long"][sym] = bool(side_is_long)
                print(
                    f"{grid.DIM}{sym} side={'LONG' if side_is_long else 'SHORT'} "
                    f"· reduce-wick={'↑ resistance' if side_is_long else '↓ support'}"
                    f"{grid.RESET}"
                )
            else:
                state["is_long"][sym] = None
    except Exception as exc:
        print(f"{grid.YELLOW}Could not cache position side ({exc}){grid.RESET}")
    print()  # blank line reserved for live ticker

    def _update_extremes(sym: str, px: float) -> None:
        """Track hi/lo over a rolling window (not since process start)."""
        if px <= 0:
            return
        now = time.time()
        hist: list[tuple[float, float]] = state["px_hist"][sym]
        hist.append((now, px))
        cut = now - extreme_window_sec
        # Keep recent samples only
        if len(hist) > 20_000 or (hist and hist[0][0] < cut):
            hist[:] = [(t, p) for t, p in hist if t >= cut]
        prices = [p for _, p in hist] or [px]
        state["hi"][sym] = max(prices)
        state["lo"][sym] = min(prices)

    def _ext_up(sym: str) -> float:
        mark = float(state["mark"].get(sym) or 0)
        hi = float(state["hi"].get(sym) or 0)
        if mark <= 0 or hi <= mark:
            return 0.0
        return (hi - mark) / mark * 100.0

    def _ext_dn(sym: str) -> float:
        mark = float(state["mark"].get(sym) or 0)
        lo = float(state["lo"].get(sym) or 0)
        if mark <= 0 or lo <= 0 or lo >= mark:
            return 0.0
        return (mark - lo) / mark * 100.0

    def _paint(sym: str | None = None, *, force: bool = False) -> None:
        """Overwrite a single live status line (\\r)."""
        now = time.time()
        if not force and now - float(state["last_paint"]) < 0.2:
            return
        state["last_paint"] = now
        sym = sym or symbols[0]
        mark = float(state["mark"].get(sym) or 0)
        hi = float(state["hi"].get(sym) or 0)
        lo = float(state["lo"].get(sym) or 0)
        up = _ext_up(sym)
        dn = _ext_dn(sym)
        ticks = int(state["ticks"].get(sym) or 0)
        kln = int(state["klines"].get(sym) or 0)
        left = max(0.0, interval - (now - float(state["last_full"])))
        st = str(state.get("status") or "listening")
        ev = str(state.get("last_event") or "")
        if st == "executing":
            st_c = grid.YELLOW
        elif st == "approved":
            st_c = grid.GREEN
        elif st == "watching":
            st_c = grid.CYAN
        else:
            st_c = grid.DIM
        # Highlight extension when near/over wick threshold
        up_s = f"{grid.GREEN}↑{up:.2f}%{grid.RESET}" if up >= min_wick else f"↑{up:.2f}%"
        dn_s = f"{grid.RED}↓{dn:.2f}%{grid.RESET}" if dn >= min_wick else f"↓{dn:.2f}%"
        ts = time.strftime("%H:%M:%S")
        line = (
            f"\r{grid.DIM}{ts}{grid.RESET} {grid.BOLD}{sym}{grid.RESET}  "
            f"mark {grid.price_fmt(mark) if mark else '—':>10}  "
            f"hi {grid.price_fmt(hi) if hi else '—':>10}  "
            f"lo {grid.price_fmt(lo) if lo else '—':>10}  "
            f"{up_s} {dn_s}  "
            f"ticks {ticks} k {kln}  sync {left:4.0f}s  "
            f"{st_c}[{st}]{grid.RESET} {grid.DIM}{ev}{grid.RESET}   "
        )
        with paint_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _break_ticker() -> None:
        with paint_lock:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _react_worker(sym: str, reason: str, force_full: bool, cycle: int) -> None:
        """REST work off the WS thread so ping/pong keeps flowing."""
        mk = state["mark"].get(sym) or 0
        hi = state["hi"].get(sym) or 0
        lo = state["lo"].get(sym) or 0
        try:
            _break_ticker()
            ts = time.strftime("%H:%M:%S")
            kind = "FULL SYNC" if force_full else "WICK/TOUCH"
            print(
                f"{grid.BOLD}{grid.GREEN}▶ APPROVED{grid.RESET}  {ts}  {sym}  ·  {kind}  ·  {reason}"
            )
            print(
                f"{grid.DIM}   mark={grid.price_fmt(mk)}  hi={grid.price_fmt(hi)}  "
                f"lo={grid.price_fmt(lo)}  ↑{_ext_up(sym):.2f}%  ↓{_ext_dn(sym):.2f}%  "
                f"cycle={cycle}{grid.RESET}"
            )
            state["status"] = "executing"
            state["last_event"] = "run_symbol…"
            _paint(sym, force=True)
            _break_ticker()
            print(f"{grid.BOLD}{grid.YELLOW}⚡ EXECUTE{grid.RESET}  {sym} …")
            rc = 1
            try:
                with state["lock"]:
                    args._live_mark = state["mark"].get(sym) or None
                    args._live_hi = state["hi"].get(sym) or None
                    args._live_lo = state["lo"].get(sym) or None
                rc = run_symbol(sym, args)
                # Refresh side cache after each cycle
                try:
                    api, sec = grid.load_keys(args.env_file)
                    recv = args.recv_window
                    hedge = False
                    try:
                        resp = grid._signed_request(
                            "GET", "/fapi/v1/positionSide/dual", {}, api, sec, recv,
                        )
                        hedge = bool(resp.get("dualSidePosition"))
                    except Exception:
                        pass
                    side_is_long, qty, _e = grid._detect_open_side(
                        sym, hedge, api, sec, recv,
                    )
                    if side_is_long is not None and qty > 0:
                        state["is_long"][sym] = bool(side_is_long)
                except Exception:
                    pass
                if force_full:
                    state["last_full"] = time.time()
            except Exception as exc:
                print(f"{grid.RED}WS react error: {exc}{grid.RESET}")
                rc = 1
            ok = rc == 0
            tag = f"{grid.GREEN}✓ DONE{grid.RESET}" if ok else f"{grid.YELLOW}· DONE{grid.RESET}"
            print(
                f"{tag}  {sym}  rc={rc}  ·  back to live ticker"
                f"{'  (dry-run)' if args.dry_run else ''}"
            )
            print()
        finally:
            with state["lock"]:
                state["busy"] = False
                args._live_mark = None
                args._live_hi = None
                args._live_lo = None
                state["status"] = "listening"
                state["last_event"] = f"done rc={locals().get('rc', 1)}"
            _paint(sym, force=True)

    def _react(sym: str, reason: str, *, force_full: bool = False) -> None:
        """Queue a react on a worker thread (never block the WS reader)."""
        now = time.time()
        with state["lock"]:
            if state["busy"] or state["stop"]:
                return
            if not force_full and now - state["last_react"].get(sym, 0) < 1.5:
                return
            state["busy"] = True
            state["last_react"][sym] = now
            state["cycle"] += 1
            cycle = state["cycle"]
            state["status"] = "approved"
            state["last_event"] = reason
        threading.Thread(
            target=_react_worker,
            args=(sym, reason, force_full, cycle),
            name=f"cr-react-{sym}",
            daemon=True,
        ).start()

    def _maybe_wick(sym: str) -> None:
        """LONG reduces on wick↑ (resistance reject); SHORT on wick↓ (support reject).

        Do NOT fire the opposite wick as a reduce — that was selling into strength
        the wrong way / spamming stale session lows while price rallied.
        """
        mark = float(state["mark"].get(sym) or 0)
        hi = float(state["hi"].get(sym) or 0)
        lo = float(state["lo"].get(sym) or 0)
        is_long = state["is_long"].get(sym)
        if mark <= 0 or is_long is None:
            return
        up = _ext_up(sym)
        dn = _ext_dn(sym)

        # Status: only highlight the reduce-relevant side
        if is_long and up >= min_wick * 0.6 and not state["busy"]:
            state["status"] = "watching"
            state["last_event"] = f"liq↑ {grid.price_fmt(hi)} (+{up:.2f}%)"
        elif (not is_long) and dn >= min_wick * 0.6 and not state["busy"]:
            state["status"] = "watching"
            state["last_event"] = f"liq↓ {grid.price_fmt(lo)} (-{dn:.2f}%)"
        elif not state["busy"] and state["status"] == "watching":
            state["status"] = "listening"
            state["last_event"] = "tick"

        if is_long:
            # Reduce only after upside liquidity grab + reject back down
            if hi > mark and up >= min_wick and mark < hi * (1.0 - break_pct / 100.0):
                if not _already_chased_wick(sym, hi):
                    _mark_wick_chased(sym, hi)  # reserve before worker (avoid spam)
                    _react(sym, f"wick↑ reduce {grid.price_fmt(hi)} (+{up:.2f}%)")
            return

        # SHORT: reduce only after downside grab + reject back up
        if lo > 0 and lo < mark and dn >= min_wick and mark > lo * (1.0 + break_pct / 100.0):
            if not _already_chased_wick(sym, lo):
                _mark_wick_chased(sym, lo)
                _react(sym, f"wick↓ reduce {grid.price_fmt(lo)} (-{dn:.2f}%)")

    def on_message(_ws: Any, message: str) -> None:
        if state["stop"]:
            return
        try:
            msg = json.loads(message)
            data = msg.get("data") or msg
            stream = str(msg.get("stream") or "")
            ev = str(data.get("e") or "")

            if "bookTicker" in stream or ev == "bookTicker":
                sym = str(data.get("s") or data.get("ps") or "").upper()
                if sym not in state["mark"]:
                    return
                try:
                    bid = float(data.get("b") or 0)
                    ask = float(data.get("a") or 0)
                except (TypeError, ValueError):
                    return
                if bid <= 0 or ask <= 0:
                    return
                mark = (bid + ask) / 2.0
                state["mark"][sym] = mark
                state["ticks"][sym] = int(state["ticks"][sym]) + 1
                _update_extremes(sym, mark)
                if not state["busy"]:
                    state["last_event"] = f"bid {grid.price_fmt(bid)} ask {grid.price_fmt(ask)}"
                _paint(sym)
                _maybe_wick(sym)
                if time.time() - state["last_full"] >= interval:
                    _react(sym, "full-sync", force_full=True)
                return

            if "markPrice" in stream or ev == "markPriceUpdate":
                sym = str(data.get("s") or "").upper()
                if sym not in state["mark"]:
                    return
                try:
                    mark = float(data.get("p") or 0)
                except (TypeError, ValueError):
                    return
                if mark <= 0:
                    return
                state["mark"][sym] = mark
                state["ticks"][sym] = int(state["ticks"][sym]) + 1
                _update_extremes(sym, mark)
                if not state["busy"]:
                    state["last_event"] = "mark"
                _paint(sym)
                _maybe_wick(sym)
                if time.time() - state["last_full"] >= interval:
                    _react(sym, "full-sync", force_full=True)
                return

            if "kline" in stream or ev == "kline":
                k = data.get("k") or {}
                sym = str(data.get("s") or k.get("s") or "").upper()
                if sym not in state["hi"]:
                    return
                try:
                    kh = float(k.get("h") or 0)
                    kl = float(k.get("l") or 0)
                    kc = float(k.get("c") or 0)
                except (TypeError, ValueError):
                    return
                state["klines"][sym] = int(state["klines"][sym]) + 1
                if kc > 0:
                    state["mark"][sym] = state["mark"][sym] or kc
                    _update_extremes(sym, kc)
                if kh > 0:
                    _update_extremes(sym, kh)
                if kl > 0:
                    _update_extremes(sym, kl)
                if k.get("x"):
                    # New candle: soft-reset window around this bar
                    now = time.time()
                    state["px_hist"][sym] = [(now, p) for p in (kl, kc, kh) if p and p > 0]
                    seed = kc or state["mark"][sym] or kh or kl
                    if seed:
                        _update_extremes(sym, seed)
                    if not state["busy"]:
                        state["last_event"] = "kline-close"
                elif not state["busy"]:
                    state["last_event"] = "kline"
                _paint(sym)
                _maybe_wick(sym)
        except Exception as exc:
            state["last_event"] = f"err {exc}"
            _paint(symbols[0], force=True)

    def on_error(_ws: Any, error: Any) -> None:
        _break_ticker()
        print(f"{grid.RED}WS error: {error}{grid.RESET}")

    def on_close(_ws: Any, status: Any, msg: Any) -> None:
        _break_ticker()
        print(f"{grid.YELLOW}WS closed ({status} {msg}) — will reconnect…{grid.RESET}")

    def on_open(_ws: Any) -> None:
        _break_ticker()
        print(
            f"{grid.GREEN}✓ WS connected{grid.RESET} — live ticker below "
            f"(▶ APPROVED / ⚡ EXECUTE when wick fires)"
        )
        state["status"] = "listening"
        state["last_event"] = "connected"
        _paint(symbols[0], force=True)

    def _run_forever() -> None:
        import ssl

        # No client-originated pings: Binance sends server pings; answering those
        # is enough. Client ping_interval + blocking REST used to cause
        # "ping/pong timed out" and drop the socket.
        while not state["stop"]:
            ws_app = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            state["ws_app"] = ws_app
            try:
                ws_app.run_forever(
                    ping_interval=0,
                    sslopt={"cert_reqs": ssl.CERT_NONE},
                )
            except Exception as exc:
                _break_ticker()
                print(f"{grid.RED}WS run error: {exc}{grid.RESET}")
            if state["stop"]:
                break
            _break_ticker()
            print(f"{grid.DIM}Reconnecting WS in 3s…{grid.RESET}")
            time.sleep(3.0)

    thread = threading.Thread(target=_run_forever, name="cr-ws", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            if not state["busy"]:
                _paint(symbols[0])
            time.sleep(0.25)
    except KeyboardInterrupt:
        state["stop"] = True
        try:
            app = state.get("ws_app")
            if app is not None:
                app.close()
        except Exception:
            pass
        _break_ticker()
        print(f"\n{grid.YELLOW}WS watch stopped.{grid.RESET}")
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.ws:
        return run_ws_watch(args)
    if not args.watch:
        return _run_once_all(args)

    interval = float(args.interval)
    syms = ", ".join(s.upper() for s in args.symbols)
    print(
        f"{grid.BOLD}{grid.CYAN}capital-reduce --watch"
        f"{' --structure' if args.structure else ''}{grid.RESET}  "
        f"{syms}  ·  every {interval:g}s  ·  Ctrl+C to stop"
    )
    if args.dry_run:
        print(f"{grid.YELLOW}(--dry-run: will not place/cancel orders){grid.RESET}")
    cycle = 0
    try:
        while True:
            cycle += 1
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{grid.DIM}── watch cycle {cycle} · {ts} ──{grid.RESET}")
            try:
                _run_once_all(args)
            except Exception as exc:
                print(f"{grid.RED}Watch cycle error: {exc}{grid.RESET}")
            print(f"{grid.DIM}Sleeping {interval:g}s…{grid.RESET}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{grid.YELLOW}Watch stopped.{grid.RESET}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
