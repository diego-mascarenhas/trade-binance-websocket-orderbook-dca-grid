#!/usr/bin/env python3
"""15s micro-grid scalp: place FULL grid + exchange TP/SL at open.

Default grid mode is **Fibonacci** (5m swing impulse → retrace levels).
Geometric ``--grid-mode step`` remains available as fallback.

On each entry cycle (when flat):
  1. MARKET base size
  2. LIMIT adds (Fib retraces or geometric steps) — complete grid
  3. TAKE_PROFIT_MARKET + STOP_MARKET (exchange algos)

Optional ``--sweep``: when a grid level fills, re-place it one step further
(barrido). Launch wiring comes later — run this file directly for now.

Usage:
  python3 orderbook_micro_grid.py LDOUSDT --dry-run
  python3 orderbook_micro_grid.py LDOUSDT --execute --direction long
  python3 orderbook_micro_grid.py LDOUSDT --grid-mode step --step-pct 0.08

Env (optional):
  OB_MG_GRID_MODE=fib  OB_MG_FIB_INTERVAL=5m  OB_MG_FIB_MIN_RANGE=0.40
  OB_MG_FVG_MIN_PCT=0.25  OB_MG_REQUIRE_FVG=1
  OB_MG_BASE_SIZE=10  OB_MG_LEVEL_SIZE=8  OB_MG_BAR_SEC=15
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from futures_scan import FAPI_BASE, fetch_klines
from ob_bars import BarBuilder, depth_to_levels
from ob_signals import SignalConfig, entry_signal
from orderbook_dca_grid import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    _dec_places,
    _resolve_hedge,
    _round_to,
    _signed_request,
    fetch_depth,
    get_position,
    get_position_meta,
    load_env_file,
    load_keys,
    load_symbol_filters,
    market_close_position,
    price_fmt,
)

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"

# Client-id prefixes — distinct from obdca / obscalp
GRID_PREFIX = "obmgG"
ENTRY_PREFIX = "obmgE"
ALGO_PREFIX = "obmg"


# ── helpers ──────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_float(name, float(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def journal_path(symbol: str) -> Path:
    return LOG_ROOT / symbol.upper() / "micro_grid.log"


def append_journal(symbol: str, message: str) -> None:
    path = journal_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def qty_for_notional(notional: float, price: float, filt: dict[str, Decimal]) -> tuple[str, float]:
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    if price <= 0:
        raise ValueError("invalid price")
    qty_d = _round_to(notional / price, step, ROUND_DOWN)
    if qty_d < filt["min_qty"]:
        qty_d = filt["min_qty"]
    while qty_d * Decimal(str(price)) < filt["min_notional"]:
        qty_d += step
    return f"{qty_d:.{qty_dp}f}", float(qty_d)


def grid_cid(symbol: str, idx: int) -> str:
    return f"{GRID_PREFIX}{symbol.upper()}{idx:02d}"[:36]


def entry_cid(symbol: str) -> str:
    ts = int(time.time()) % 1_000_000
    return f"{ENTRY_PREFIX}{symbol.upper()}{ts}"[:36]


def algo_cid(tag: str, symbol: str) -> str:
    return f"{ALGO_PREFIX}{tag}{symbol.upper()}"[:36]


# ── grid math ────────────────────────────────────────────────────────────────

@dataclass
class GridLevel:
    idx: int
    name: str
    price: float
    size_usdt: float
    qty: float
    dist_pct: float
    fib_ratio: float | None = None


@dataclass
class SwingImpulse:
    """Detected HTF impulse used as Fib anchor."""

    is_long: bool  # True = bullish impulse (grid buys on pullback)
    low: float
    high: float
    range_pct: float
    start_i: int
    end_i: int
    interval: str

    @property
    def span(self) -> float:
        return max(0.0, self.high - self.low)


@dataclass
class FvgZone:
    """3-candle fair value gap on the Fib interval."""

    is_long: bool  # bullish imbalance (buy-side gap)
    low: float
    high: float
    size_pct: float
    index: int  # first candle of the 3-candle pattern

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class GridPlan:
    is_long: bool
    entry: float
    base_usdt: float
    base_qty: float
    levels: list[GridLevel]
    tp_price: float
    sl_price: float
    planned_qty: float
    planned_avg: float
    mode: str = "step"
    note: str = ""
    swing: SwingImpulse | None = None
    fvg: FvgZone | None = None


# Default Fib retracement set (closest → deepest). Truncated by --levels.
FIB_RETRACES = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_TP_EXT = 1.272  # extension beyond impulse end
FIB_SL_BUF = 0.15   # % beyond swing origin
FVG_MIN_PCT = 0.25  # min FVG height as % of mid price


def detect_swing_impulse(
    symbol: str,
    *,
    prefer_long: bool | None,
    interval: str = "5m",
    lookback: int = 40,
    min_range_pct: float = 0.40,
    max_span_bars: int = 12,
    base: str = FAPI_BASE,
) -> SwingImpulse | None:
    """Find the strongest recent impulse on ``interval`` klines (drop forming bar)."""
    raw = fetch_klines(base, symbol.upper(), interval, max(lookback, 20))
    if len(raw) < 8:
        return None
    closed = raw[:-1]
    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    n = len(closed)
    start = max(0, n - lookback)

    best_bull: tuple[float, SwingImpulse] | None = None
    best_bear: tuple[float, SwingImpulse] | None = None

    for i in range(start, n):
        for j in range(i + 1, min(n, i + max_span_bars + 1)):
            window_lows = lows[i : j + 1]
            window_highs = highs[i : j + 1]
            lo = min(window_lows)
            hi = max(window_highs)
            if lo <= 0 or hi <= lo:
                continue
            lo_i = i + window_lows.index(lo)
            hi_i = i + window_highs.index(hi)
            rng = (hi - lo) / lo * 100
            if rng < min_range_pct:
                continue
            recency = 1.0 + 0.08 * max(0, j - (n - 8))  # boost swings ending near now
            score = rng * recency
            if lo_i < hi_i:
                cand = SwingImpulse(
                    is_long=True, low=lo, high=hi, range_pct=rng,
                    start_i=lo_i, end_i=hi_i, interval=interval,
                )
                if best_bull is None or score > best_bull[0]:
                    best_bull = (score, cand)
            if hi_i < lo_i:
                cand = SwingImpulse(
                    is_long=False, low=lo, high=hi, range_pct=rng,
                    start_i=hi_i, end_i=lo_i, interval=interval,
                )
                if best_bear is None or score > best_bear[0]:
                    best_bear = (score, cand)

    bull = best_bull[1] if best_bull else None
    bear = best_bear[1] if best_bear else None
    if prefer_long is True:
        return bull or bear
    if prefer_long is False:
        return bear or bull
    if bull and bear:
        bs = best_bull[0] if best_bull else 0.0
        rs = best_bear[0] if best_bear else 0.0
        return bull if bs >= rs else bear
    return bull or bear


def detect_fvg_zones(
    symbol: str,
    *,
    interval: str = "5m",
    lookback: int = 40,
    min_size_pct: float = FVG_MIN_PCT,
    base: str = FAPI_BASE,
) -> list[FvgZone]:
    """All 3-candle FVGs on closed bars with height ≥ ``min_size_pct``."""
    raw = fetch_klines(base, symbol.upper(), interval, max(lookback, 20))
    if len(raw) < 6:
        return []
    closed = raw[:-1]
    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    out: list[FvgZone] = []
    start = max(1, len(closed) - lookback)
    for i in range(start, len(closed) - 2):
        # Bullish: candle1 high < candle3 low
        gap_lo = highs[i]
        gap_hi = lows[i + 2]
        if gap_hi > gap_lo:
            mid = (gap_lo + gap_hi) / 2.0
            size = (gap_hi - gap_lo) / mid * 100 if mid > 0 else 0.0
            if size >= min_size_pct:
                out.append(FvgZone(True, gap_lo, gap_hi, size, i))
        # Bearish: candle1 low > candle3 high
        gap_hi_b = lows[i]
        gap_lo_b = highs[i + 2]
        if gap_hi_b > gap_lo_b:
            mid = (gap_lo_b + gap_hi_b) / 2.0
            size = (gap_hi_b - gap_lo_b) / mid * 100 if mid > 0 else 0.0
            if size >= min_size_pct:
                out.append(FvgZone(False, gap_lo_b, gap_hi_b, size, i))
    return out


def select_fvg_for_trade(
    zones: list[FvgZone],
    *,
    is_long: bool,
    entry: float,
    swing: SwingImpulse | None,
) -> FvgZone | None:
    """Pick the best unfilled FVG aligned with trade side (prefer near swing / price)."""
    side = [z for z in zones if z.is_long == is_long]
    if not side:
        return None

    def _score(z: FvgZone) -> float:
        # Prefer larger gaps closer to entry; bonus if overlaps swing range
        dist = abs(z.mid - entry) / entry * 100 if entry > 0 else 99.0
        score = z.size_pct * 2.0 - dist
        if swing is not None:
            # Overlap with impulse body
            overlap = not (z.high < swing.low or z.low > swing.high)
            if overlap:
                score += 1.5
            # Prefer FVG formed near impulse end
            if abs(z.index - swing.end_i) <= 3:
                score += 1.0
        # Still "in play": long FVG not fully traded through above; short not below
        if is_long and entry < z.low - (z.high - z.low):
            score -= 2.0  # price already far below gap (unlikely fill path)
        if not is_long and entry > z.high + (z.high - z.low):
            score -= 2.0
        return score

    return max(side, key=_score)


def build_fib_grid(
    entry: float,
    is_long: bool,
    swing: SwingImpulse,
    *,
    levels: int,
    base_usdt: float,
    level_usdt: float,
    tp_pct: float,
    sl_pct: float,
    filt: dict[str, Decimal],
    tp_ext: float = FIB_TP_EXT,
    sl_buf_pct: float = FIB_SL_BUF,
    fvg: FvgZone | None = None,
) -> GridPlan:
    """Retrace grid on swing; only levels on the adverse side of ``entry``."""
    if entry <= 0 or swing.span <= 0:
        raise ValueError("invalid entry/swing for fib grid")
    _, base_qty = qty_for_notional(base_usdt, entry, filt)
    n_take = max(1, min(int(levels), len(FIB_RETRACES)))
    ratios = list(FIB_RETRACES[:n_take])

    rows: list[GridLevel] = []
    for i, r in enumerate(ratios, start=1):
        if is_long:
            px = swing.high - swing.span * r
            # Must be below entry (pullback buys)
            if px >= entry * 0.9995:
                continue
        else:
            px = swing.low + swing.span * r
            if px <= entry * 1.0005:
                continue
        _, qty = qty_for_notional(level_usdt, px, filt)
        dist = (px / entry - 1.0) * 100
        rows.append(
            GridLevel(
                idx=i,
                name=f"Fib {r:.3f}",
                price=px,
                size_usdt=float(qty) * px,
                qty=qty,
                dist_pct=dist,
                fib_ratio=r,
            )
        )
    # Re-index after skips
    for i, lv in enumerate(rows, start=1):
        lv.idx = i

    cum_q = base_qty
    cum_n = float(base_qty) * entry
    for lv in rows:
        cum_q += lv.qty
        cum_n += lv.size_usdt
    planned_avg = cum_n / cum_q if cum_q > 0 else entry

    # TP: max(extension, pct from entry); SL: beyond swing origin
    if is_long:
        tp_ext_px = swing.high + swing.span * max(0.0, tp_ext - 1.0)
        tp_pct_px = entry * (1 + tp_pct / 100)
        tp = max(tp_ext_px, tp_pct_px)
        sl_swing = swing.low * (1 - sl_buf_pct / 100)
        sl_pct_px = entry * (1 - sl_pct / 100)
        sl = min(sl_swing, sl_pct_px)
        if rows:
            deepest = min(lv.price for lv in rows)
            sl = min(sl, deepest * (1 - sl_buf_pct / 100))
    else:
        tp_ext_px = swing.low - swing.span * max(0.0, tp_ext - 1.0)
        tp_pct_px = entry * (1 - tp_pct / 100)
        tp = min(tp_ext_px, tp_pct_px)
        sl_swing = swing.high * (1 + sl_buf_pct / 100)
        sl_pct_px = entry * (1 + sl_pct / 100)
        sl = max(sl_swing, sl_pct_px)
        if rows:
            deepest = max(lv.price for lv in rows)
            sl = max(sl, deepest * (1 + sl_buf_pct / 100))

    note = (
        f"fib {swing.interval} swing "
        f"{price_fmt(swing.low)}→{price_fmt(swing.high)} "
        f"({swing.range_pct:.2f}%) · TP ext {tp_ext:g}"
    )
    if fvg is not None:
        note += (
            f" · FVG {price_fmt(fvg.low)}–{price_fmt(fvg.high)} "
            f"({fvg.size_pct:.2f}%)"
        )
    return GridPlan(
        is_long=is_long,
        entry=entry,
        base_usdt=float(base_qty) * entry,
        base_qty=base_qty,
        levels=rows,
        tp_price=tp,
        sl_price=sl,
        planned_qty=cum_q,
        planned_avg=planned_avg,
        mode="fib",
        note=note,
        swing=swing,
        fvg=fvg,
    )


def build_step_grid(
    entry: float,
    is_long: bool,
    *,
    levels: int,
    step_pct: float,
    base_usdt: float,
    level_usdt: float,
    tp_pct: float,
    sl_pct: float,
    filt: dict[str, Decimal],
) -> GridPlan:
    """Geometric adds against the position + TP/SL beyond the last rung."""
    if entry <= 0:
        raise ValueError("entry required")
    levels = max(0, int(levels))
    step_pct = max(0.01, float(step_pct))
    _, base_qty = qty_for_notional(base_usdt, entry, filt)

    rows: list[GridLevel] = []
    for i in range(1, levels + 1):
        dist = step_pct * i
        if is_long:
            px = entry * (1 - dist / 100)
        else:
            px = entry * (1 + dist / 100)
        _, qty = qty_for_notional(level_usdt, px, filt)
        rows.append(
            GridLevel(
                idx=i,
                name=f"MG #{i}",
                price=px,
                size_usdt=float(qty) * px,
                qty=qty,
                dist_pct=-dist if is_long else dist,
            )
        )

    cum_q = base_qty
    cum_n = float(base_qty) * entry
    for lv in rows:
        cum_q += lv.qty
        cum_n += lv.size_usdt
    planned_avg = cum_n / cum_q if cum_q > 0 else entry

    last_dist = step_pct * levels if levels else 0.0
    sl_dist = max(sl_pct, last_dist + step_pct * 0.5)
    if is_long:
        tp = entry * (1 + tp_pct / 100)
        sl = entry * (1 - sl_dist / 100)
    else:
        tp = entry * (1 - tp_pct / 100)
        sl = entry * (1 + sl_dist / 100)

    return GridPlan(
        is_long=is_long,
        entry=entry,
        base_usdt=float(base_qty) * entry,
        base_qty=base_qty,
        levels=rows,
        tp_price=tp,
        sl_price=sl,
        planned_qty=cum_q,
        planned_avg=planned_avg,
        mode="step",
        note=f"step {step_pct:g}% × {levels}",
    )


# Back-compat alias
build_micro_grid = build_step_grid


def build_grid_plan(
    symbol: str,
    entry: float,
    is_long: bool,
    args: argparse.Namespace,
    filt: dict[str, Decimal],
) -> GridPlan | None:
    """Build Fib (default) or step grid.

    Fib mode requires a swing and (by default) an FVG ≥ ``fvg_min_pct``.
    Returns ``None`` to skip the entry cycle when Fib quality filters fail.
    """
    mode = (getattr(args, "grid_mode", "fib") or "fib").strip().lower()
    if mode == "fib":
        prefer = True if is_long else False
        try:
            swing = detect_swing_impulse(
                symbol,
                prefer_long=prefer,
                interval=args.fib_interval,
                lookback=args.fib_lookback,
                min_range_pct=args.fib_min_range,
                max_span_bars=args.fib_max_span,
            )
        except Exception as exc:
            print(f"{YELLOW}Fib swing fetch failed ({exc}) — skip{RESET}")
            return None
        if swing is None:
            print(
                f"{YELLOW}No Fib swing (≥{args.fib_min_range:g}% on {args.fib_interval}) — skip{RESET}"
            )
            return None

        if swing.is_long != is_long:
            print(
                f"{YELLOW}Swing is {'bull' if swing.is_long else 'bear'} "
                f"but signal is {'LONG' if is_long else 'SHORT'} — "
                f"still anchoring Fib to that swing{RESET}"
            )

        fvg: FvgZone | None = None
        require_fvg = bool(getattr(args, "require_fvg", True))
        min_fvg = float(getattr(args, "fvg_min_pct", FVG_MIN_PCT))
        try:
            zones = detect_fvg_zones(
                symbol,
                interval=args.fib_interval,
                lookback=args.fib_lookback,
                min_size_pct=min_fvg,
            )
            fvg = select_fvg_for_trade(zones, is_long=is_long, entry=entry, swing=swing)
        except Exception as exc:
            print(f"{YELLOW}FVG scan failed ({exc}){RESET}")
            zones = []
            fvg = None

        if require_fvg and fvg is None:
            print(
                f"{YELLOW}No FVG ≥{min_fvg:g}% aligned with "
                f"{'LONG' if is_long else 'SHORT'} on {args.fib_interval} — skip{RESET}"
            )
            return None
        if fvg is not None:
            print(
                f"{DIM}FVG {'bull' if fvg.is_long else 'bear'} "
                f"{price_fmt(fvg.low)}–{price_fmt(fvg.high)} ({fvg.size_pct:.2f}%){RESET}"
            )

        plan = build_fib_grid(
            entry,
            is_long,
            swing,
            levels=args.levels,
            base_usdt=args.base_size,
            level_usdt=args.level_size,
            tp_pct=args.tp_pct,
            sl_pct=args.sl_pct,
            filt=filt,
            tp_ext=args.fib_tp_ext,
            sl_buf_pct=args.fib_sl_buf,
            fvg=fvg,
        )
        if not plan.levels:
            print(f"{YELLOW}Fib levels all on wrong side of mark — skip{RESET}")
            return None
        return plan

    return build_step_grid(
        entry,
        is_long,
        levels=args.levels,
        step_pct=args.step_pct,
        base_usdt=args.base_size,
        level_usdt=args.level_size,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        filt=filt,
    )


def render_plan(symbol: str, plan: GridPlan) -> str:
    side = "LONG" if plan.is_long else "SHORT"
    color = GREEN if plan.is_long else RED
    mode = plan.mode.upper()
    lines = [
        f"{BOLD}{CYAN}Micro-grid · {symbol.upper()} · {color}{side}{RESET}  {DIM}[{mode}]{RESET}",
        f"{DIM}entry {price_fmt(plan.entry)}  ·  "
        f"base {plan.base_usdt:.2f} USDT  ·  "
        f"{len(plan.levels)} levels  ·  "
        f"TP {price_fmt(plan.tp_price)}  SL {price_fmt(plan.sl_price)}  ·  "
        f"planned avg {price_fmt(plan.planned_avg)} ({plan.planned_qty:g} qty){RESET}",
    ]
    if plan.note:
        lines.append(f"{DIM}{plan.note}{RESET}")
    lines += [
        "",
        f"{'ORDER':<12} {'QTY':>12} {'PRICE':>14} {'Δ%':>8} {'USDT':>10}",
        f"{DIM}{'-' * 60}{RESET}",
        f"{'Base':<12} {plan.base_qty:>12g} {price_fmt(plan.entry):>14} {'0.00':>8} {plan.base_usdt:>10.2f}",
    ]
    for lv in plan.levels:
        lines.append(
            f"{lv.name:<12} {lv.qty:>12g} {price_fmt(lv.price):>14} "
            f"{lv.dist_pct:>+7.2f}% {lv.size_usdt:>10.2f}"
        )
    return "\n".join(lines)


# ── exchange I/O ─────────────────────────────────────────────────────────────

def market_open(
    symbol: str,
    is_long: bool,
    qty_str: str,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> dict[str, Any]:
    side = "BUY" if is_long else "SELL"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": entry_cid(symbol),
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    return _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def place_grid_limits(
    symbol: str,
    plan: GridPlan,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> int:
    """Place every grid LIMIT at once. Returns count placed."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    price_round = ROUND_DOWN if plan.is_long else ROUND_UP
    side = "BUY" if plan.is_long else "SELL"
    placed = 0
    for lv in plan.levels:
        price_d = _round_to(lv.price, tick, price_round)
        qty_d = _round_to(lv.qty, step, ROUND_DOWN)
        if qty_d < filt["min_qty"]:
            qty_d = filt["min_qty"]
        while qty_d * price_d < filt["min_notional"]:
            qty_d += step
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty_d:.{qty_dp}f}",
            "price": f"{price_d:.{price_dp}f}",
            "newClientOrderId": grid_cid(symbol, lv.idx),
        }
        if hedge:
            params["positionSide"] = "LONG" if plan.is_long else "SHORT"
        try:
            _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)
            print(
                f"{GREEN}✓ {lv.name} LIMIT {side} {params['quantity']} @ {params['price']}{RESET}"
            )
            placed += 1
        except Exception as exc:
            print(f"{RED}✗ {lv.name} failed: {exc}{RESET}")
    return placed


def list_open_orders(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    try:
        resp = _signed_request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
        )
    except Exception:
        return []
    return resp if isinstance(resp, list) else []


def list_open_algos(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    try:
        resp = _signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv,
        )
    except Exception:
        return []
    if isinstance(resp, list):
        return resp
    return list(resp.get("orders") or resp.get("data") or [])


def _order_cid(o: dict) -> str:
    return str(o.get("clientOrderId") or o.get("origClientOrderId") or "")


def _algo_cid_of(o: dict) -> str:
    return str(o.get("clientAlgoId") or o.get("newClientOrderId") or "")


def cancel_our_grid(symbol: str, api: str, sec: str, recv: int) -> int:
    sym = symbol.upper()
    killed = 0
    for o in list_open_orders(symbol, api, sec, recv):
        cid = _order_cid(o)
        if not (cid.startswith(GRID_PREFIX) and sym in cid):
            continue
        try:
            _signed_request(
                "DELETE", "/fapi/v1/order",
                {"symbol": sym, "orderId": o.get("orderId")},
                api, sec, recv,
            )
            killed += 1
        except Exception:
            pass
    return killed


def cancel_our_exits(symbol: str, api: str, sec: str, recv: int) -> int:
    sym = symbol.upper()
    killed = 0
    for o in list_open_algos(symbol, api, sec, recv):
        cid = _algo_cid_of(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        try:
            _signed_request(
                "DELETE", "/fapi/v1/algoOrder",
                {"symbol": sym, "algoId": o.get("algoId")},
                api, sec, recv,
            )
            killed += 1
        except Exception:
            pass
    return killed


def place_exchange_exits(
    symbol: str,
    is_long: bool,
    qty: float,
    entry: float,
    mark: float,
    tp_price: float,
    sl_price: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    fee_buffer_pct: float = 0.12,
) -> tuple[float, float]:
    """Arm TAKE_PROFIT_MARKET + STOP_MARKET for current (or planned) qty."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    qty_d = _round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        raise ValueError("qty too small for exits")
    qty_str = f"{qty_d:.{qty_dp}f}"

    tp, sl = float(tp_price), float(sl_price)
    min_tp = max(fee_buffer_pct * 2.0, 0.30)
    if entry > 0 and abs(tp - entry) / entry * 100 < min_tp:
        tp = entry * (1 + min_tp / 100) if is_long else entry * (1 - min_tp / 100)

    # Nudge off mark
    pad = max(float(tick) * 2, mark * max(fee_buffer_pct, 0.05) / 100.0)
    if is_long:
        if mark >= tp - pad:
            tp = mark + pad
        if mark <= sl + pad:
            sl = mark - pad
        tp_d = _round_to(tp, tick, ROUND_UP)
        sl_d = _round_to(sl, tick, ROUND_DOWN)
    else:
        if mark <= tp + pad:
            tp = mark - pad
        if mark >= sl - pad:
            sl = mark + pad
        tp_d = _round_to(tp, tick, ROUND_DOWN)
        sl_d = _round_to(sl, tick, ROUND_UP)

    tp_str = f"{tp_d:.{price_dp}f}"
    sl_str = f"{sl_d:.{price_dp}f}"
    close_side = "SELL" if is_long else "BUY"

    cancel_our_exits(symbol, api, sec, recv)

    def _one(order_type: str, trigger: str, tag: str) -> dict:
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol.upper(),
            "side": close_side,
            "type": order_type,
            "quantity": qty_str,
            "triggerPrice": trigger,
            "workingType": "CONTRACT_PRICE",
            "clientAlgoId": algo_cid(tag, symbol),
        }
        if hedge:
            params["positionSide"] = "LONG" if is_long else "SHORT"
        else:
            params["reduceOnly"] = "true"
        return _signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)

    tp_resp = _one("TAKE_PROFIT_MARKET", tp_str, "TP")
    print(f"{GREEN}✓ Exchange TP {close_side} {qty_str} @ {tp_str} (algoId={tp_resp.get('algoId')}){RESET}")
    sl_resp = _one("STOP_MARKET", sl_str, "SL")
    print(f"{GREEN}✓ Exchange SL {close_side} {qty_str} @ {sl_str} (algoId={sl_resp.get('algoId')}){RESET}")
    return float(tp_d), float(sl_d)


# ── cycle state ──────────────────────────────────────────────────────────────

@dataclass
class CycleState:
    active: bool = False
    is_long: bool = True
    entry: float = 0.0
    plan: GridPlan | None = None
    tp: float = 0.0
    sl: float = 0.0
    last_qty: float = 0.0
    filled_levels: set[int] = field(default_factory=set)
    opened_at: float = 0.0


def open_cycle(
    symbol: str,
    plan: GridPlan,
    mark: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    dry_run: bool,
) -> CycleState:
    """Place base MARKET + full LIMIT grid + TP/SL in one shot."""
    print(render_plan(symbol, plan))
    append_journal(
        symbol,
        f"PLAN {plan.mode} {'LONG' if plan.is_long else 'SHORT'} entry={plan.entry:.8g} "
        f"levels={len(plan.levels)} tp={plan.tp_price:.8g} sl={plan.sl_price:.8g} "
        f"{plan.note}",
    )

    if dry_run:
        print(f"{YELLOW}Dry-run — no orders sent{RESET}")
        return CycleState(active=False)

    qty_str, _ = qty_for_notional(plan.base_usdt, plan.entry, filt)
    print(f"\n{BOLD}Opening MARKET base {qty_str}…{RESET}")
    market_open(symbol, plan.is_long, qty_str, hedge, api, sec, recv)
    time.sleep(0.35)

    qty, entry = get_position(symbol, plan.is_long, hedge, api, sec, recv)
    if qty <= 0 or entry <= 0:
        # fallback to plan
        entry = plan.entry
        qty = plan.base_qty
    print(f"{GREEN}✓ Base filled ~{qty:g} @ {price_fmt(entry)}{RESET}")
    append_journal(symbol, f"OPEN {'LONG' if plan.is_long else 'SHORT'} qty={qty:g} entry={entry:.8g}")

    print(f"{BOLD}Placing full grid ({len(plan.levels)} limits)…{RESET}")
    n = place_grid_limits(symbol, plan, filt, hedge, api, sec, recv)
    append_journal(symbol, f"GRID placed={n}/{len(plan.levels)}")

    # Arm exits for current qty (refresh later if adds fill)
    print(f"{BOLD}Arming exchange TP/SL…{RESET}")
    if plan.entry > 0 and entry > 0:
        scale = entry / plan.entry
        tp_p = plan.tp_price * scale
        sl_p = plan.sl_price * scale
    else:
        tp_p, sl_p = plan.tp_price, plan.sl_price

    tp, sl = place_exchange_exits(
        symbol, plan.is_long, qty, entry, mark or entry,
        tp_p, sl_p, filt, hedge, api, sec, recv,
    )
    append_journal(symbol, f"EXITS tp={tp:.8g} sl={sl:.8g} qty={qty:g}")

    return CycleState(
        active=True,
        is_long=plan.is_long,
        entry=entry,
        plan=plan,
        tp=tp,
        sl=sl,
        last_qty=qty,
        opened_at=time.time(),
    )


def refresh_exits_if_grown(
    symbol: str,
    state: CycleState,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    mark: float,
    args: argparse.Namespace,
) -> None:
    """When DCA fills grow qty, re-arm TP/SL for full position (and optional sweep)."""
    if not state.active or state.plan is None:
        return
    qty, entry = get_position(symbol, state.is_long, hedge, api, sec, recv)
    if qty <= 0:
        return

    grown = qty > state.last_qty * 1.02
    if grown:
        print(f"{CYAN}Position grew {state.last_qty:g} → {qty:g} — refreshing TP/SL{RESET}")
        # TP from live avg entry; keep SL from plan relative to original entry band
        if state.is_long:
            tp = entry * (1 + args.tp_pct / 100)
            sl = state.sl if state.sl > 0 else entry * (1 - args.sl_pct / 100)
            if sl >= entry:
                sl = entry * (1 - args.sl_pct / 100)
        else:
            tp = entry * (1 - args.tp_pct / 100)
            sl = state.sl if state.sl > 0 else entry * (1 + args.sl_pct / 100)
            if sl <= entry:
                sl = entry * (1 + args.sl_pct / 100)
        try:
            tp, sl = place_exchange_exits(
                symbol, state.is_long, qty, entry, mark or entry,
                tp, sl, filt, hedge, api, sec, recv,
            )
            state.tp, state.sl = tp, sl
            state.entry = entry
            append_journal(symbol, f"EXITS refresh tp={tp:.8g} sl={sl:.8g} qty={qty:g}")
        except Exception as exc:
            print(f"{RED}Exit refresh failed: {exc}{RESET}")
        state.last_qty = qty

        if args.sweep:
            _sweep_missing_levels(symbol, state, filt, hedge, api, sec, recv, args)


def _sweep_missing_levels(
    symbol: str,
    state: CycleState,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> None:
    """Re-place filled grid rungs one step further (barrido)."""
    if state.plan is None:
        return
    open_cids = {_order_cid(o) for o in list_open_orders(symbol, api, sec, recv)}
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    price_round = ROUND_DOWN if state.is_long else ROUND_UP
    side = "BUY" if state.is_long else "SELL"

    for lv in state.plan.levels:
        cid = grid_cid(symbol, lv.idx)
        if cid in open_cids:
            continue
        if lv.idx in state.filled_levels:
            continue
        state.filled_levels.add(lv.idx)
        # Push one extra step beyond this level
        extra = args.step_pct
        if state.is_long:
            px = lv.price * (1 - extra / 100)
        else:
            px = lv.price * (1 + extra / 100)
        price_d = _round_to(px, tick, price_round)
        qty_d = _round_to(lv.qty, step, ROUND_DOWN)
        if qty_d < filt["min_qty"]:
            qty_d = filt["min_qty"]
        while qty_d * price_d < filt["min_notional"]:
            qty_d += step
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty_d:.{qty_dp}f}",
            "price": f"{price_d:.{price_dp}f}",
            "newClientOrderId": grid_cid(symbol, lv.idx),  # reuse slot
        }
        if hedge:
            params["positionSide"] = "LONG" if state.is_long else "SHORT"
        try:
            _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)
            print(f"{CYAN}↻ Sweep {lv.name} → {params['price']}{RESET}")
            append_journal(symbol, f"SWEEP level={lv.idx} price={params['price']}")
            # allow re-detect next fill
            state.filled_levels.discard(lv.idx)
        except Exception as exc:
            print(f"{YELLOW}Sweep {lv.name} skip: {exc}{RESET}")


def close_cycle_cleanup(symbol: str, state: CycleState, api: str, sec: str, recv: int) -> None:
    n_g = cancel_our_grid(symbol, api, sec, recv)
    n_e = cancel_our_exits(symbol, api, sec, recv)
    if n_g or n_e:
        print(f"{DIM}Cleanup cancelled grid={n_g} exits={n_e}{RESET}")
    append_journal(symbol, f"FLAT cleanup grid={n_g} exits={n_e}")
    state.active = False
    state.plan = None
    state.last_qty = 0.0
    state.filled_levels.clear()


# ── main loop ────────────────────────────────────────────────────────────────

def mid_from_depth(depth: dict) -> float:
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return 0.0
    return (float(bids[0][0]) + float(asks[0][0])) / 2.0


def run(args: argparse.Namespace) -> int:
    load_env_file(args.env_file)
    sym = args.symbol.upper()
    api, sec = load_keys(args.env_file)
    if args.execute and (not api or not sec):
        print(f"{RED}No API keys — cannot execute{RESET}")
        return 1

    filt = load_symbol_filters(sym)
    hedge = False
    if args.execute:
        hedge = _resolve_hedge(args, api, sec)

    sig_cfg = SignalConfig(
        imb_long=args.imb_long,
        imb_short=args.imb_short,
        momentum_min_pct=args.momentum_min_pct,
        require_momentum=True,
        use_imbalance=True,
    )
    builder = BarBuilder(bar_sec=args.bar_sec, band_pct=args.band_pct)
    state = CycleState()
    cooldown_until = 0.0

    mode = f"{GREEN}EXECUTE{RESET}" if args.execute else f"{YELLOW}DRY-RUN{RESET}"
    print(
        f"{BOLD}{CYAN}Micro-grid{RESET} {sym}  {mode}\n"
        f"{DIM}grid={args.grid_mode} · fib {args.fib_interval} minΔ{args.fib_min_range:g}% · "
        f"FVG≥{args.fvg_min_pct:g}%{' req' if args.require_fvg else ''} · "
        f"bar {args.bar_sec:g}s · levels {args.levels} · "
        f"base {args.base_size:g} · level {args.level_size:g} USDT · "
        f"TP {args.tp_pct:g}% · SL {args.sl_pct:g}% · "
        f"sweep={'ON' if args.sweep else 'OFF'} · "
        f"dir={args.direction}{RESET}\n"
        f"{DIM}log {journal_path(sym)}{RESET}"
    )
    append_journal(
        sym,
        f"START mode={args.grid_mode} bar={args.bar_sec} levels={args.levels} execute={int(args.execute)}",
    )

    builder.start_bar(time.time())
    try:
        while True:
            depth = fetch_depth(sym, args.depth_limit)
            bids, asks = depth_to_levels(depth)
            mark = mid_from_depth(depth)
            if mark <= 0:
                time.sleep(args.sample_sec)
                continue

            bar = builder.add_sample(bids, asks, time.time())

            # Live position sync
            if args.execute and state.active:
                qty, entry = get_position(sym, state.is_long, hedge, api, sec, args.recv_window)
                if qty <= 0:
                    print(f"{GREEN}Position flat — cycle done{RESET}")
                    close_cycle_cleanup(sym, state, api, sec, args.recv_window)
                    cooldown_until = time.time() + args.cooldown_sec
                else:
                    refresh_exits_if_grown(
                        sym, state, filt, hedge, api, sec, args.recv_window, mark, args,
                    )
                    meta = get_position_meta(sym, state.is_long, hedge, api, sec, args.recv_window)
                    upnl = float(meta.get("unrealized_pnl") or 0)
                    print(
                        f"{DIM}{time.strftime('%H:%M:%S')}  "
                        f"{'LONG' if state.is_long else 'SHORT'} qty={qty:g} "
                        f"entry={price_fmt(entry)} mid={price_fmt(mark)} "
                        f"uPnL={upnl:+.4f}  TP={price_fmt(state.tp)} SL={price_fmt(state.sl)}{RESET}"
                    )

            # New cycle on bar close when flat
            if bar is not None and not state.active and time.time() >= cooldown_until:
                direction: str | None
                if args.direction in ("long", "short"):
                    direction = args.direction
                else:
                    direction = entry_signal(bar, sig_cfg)

                if direction:
                    # Refuse if already exposed (other bot)
                    if args.execute:
                        ql, _ = get_position(sym, True, hedge, api, sec, args.recv_window)
                        qs, _ = get_position(sym, False, hedge, api, sec, args.recv_window)
                        if ql > 0 or qs > 0:
                            print(f"{YELLOW}Skip — existing position on {sym}{RESET}")
                            builder.reset_after_bar(time.time())
                            time.sleep(args.sample_sec)
                            continue

                    is_long = direction == "long"
                    print(
                        f"\n{CYAN}Signal {direction.upper()} "
                        f"imb={bar.imbalance:.3f} Δ={bar.mid_change_pct():+.3f}%{RESET}"
                    )
                    plan = build_grid_plan(sym, mark, is_long, args, filt)
                    if plan is None:
                        builder.reset_after_bar(time.time())
                        time.sleep(args.sample_sec)
                        continue
                    try:
                        state = open_cycle(
                            sym, plan, mark, filt, hedge, api, sec, args.recv_window,
                            dry_run=not args.execute,
                        )
                        if not args.execute:
                            # dry-run: one cycle then idle until next signal
                            cooldown_until = time.time() + args.cooldown_sec
                    except Exception as exc:
                        print(f"{RED}Open cycle failed: {exc}{RESET}")
                        append_journal(sym, f"ERROR open {exc}")
                        if args.execute:
                            cancel_our_grid(sym, api, sec, args.recv_window)
                            cancel_our_exits(sym, api, sec, args.recv_window)
                        cooldown_until = time.time() + args.cooldown_sec

                    builder.reset_after_bar(time.time())
                    if args.once:
                        return 0
                    continue

                builder.reset_after_bar(time.time())
                if args.once and direction:
                    return 0
            elif bar is not None:
                builder.reset_after_bar(time.time())
                if args.once and not state.active:
                    # no signal on first bar in --once mode: keep going until one fires
                    pass

            time.sleep(max(0.2, args.sample_sec))
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}")
        if args.execute and state.active:
            print(f"{YELLOW}Leaving position + grid/exits on book (Ctrl+C does not flatten){RESET}")
        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="15s micro-grid: full grid + TP/SL at open")
    p.add_argument("symbol", help="Futures symbol, e.g. UBUSDT")
    p.add_argument("--execute", action="store_true", help="Send live orders")
    p.add_argument("--dry-run", action="store_true", help="Plan only (default if no --execute)")
    p.add_argument("--env-file", default="", help="Path to .env")
    p.add_argument("--recv-window", type=int, default=15000)
    p.add_argument("--position-mode", choices=("auto", "hedge", "oneway"), default="auto")
    p.add_argument("--direction", choices=("auto", "long", "short"), default="auto")
    p.add_argument(
        "--grid-mode",
        choices=("fib", "step"),
        default=(os.getenv("OB_MG_GRID_MODE", "fib").strip().lower() or "fib"),
        help="Grid construction (default: fib)",
    )
    p.add_argument("--fib-interval", default=os.getenv("OB_MG_FIB_INTERVAL", "5m").strip() or "5m")
    p.add_argument("--fib-lookback", type=int, default=_env_int("OB_MG_FIB_LOOKBACK", 40))
    p.add_argument("--fib-min-range", type=float, default=_env_float("OB_MG_FIB_MIN_RANGE", 0.40),
                   help="Min swing range %% on fib interval")
    p.add_argument("--fib-max-span", type=int, default=_env_int("OB_MG_FIB_MAX_SPAN", 12))
    p.add_argument("--fib-tp-ext", type=float, default=_env_float("OB_MG_FIB_TP_EXT", FIB_TP_EXT))
    p.add_argument("--fib-sl-buf", type=float, default=_env_float("OB_MG_FIB_SL_BUF", FIB_SL_BUF),
                   help="%% buffer beyond swing origin for SL")
    p.add_argument("--fvg-min-pct", type=float, default=_env_float("OB_MG_FVG_MIN_PCT", FVG_MIN_PCT),
                   help="Min FVG height %% of mid (default 0.25)")
    p.add_argument("--require-fvg", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_MG_REQUIRE_FVG", True),
                   help="Fib mode: require FVG ≥ min (default on)")
    p.add_argument("--levels", type=int, default=_env_int("OB_MG_LEVELS", 4))
    p.add_argument("--step-pct", type=float, default=_env_float("OB_MG_STEP_PCT", 0.08),
                   help="Only for --grid-mode step")
    p.add_argument("--base-size", type=float, default=_env_float("OB_MG_BASE_SIZE", 10.0))
    p.add_argument("--level-size", type=float, default=_env_float("OB_MG_LEVEL_SIZE", 8.0))
    p.add_argument("--tp-pct", type=float, default=_env_float("OB_MG_TP_PCT", 0.35))
    p.add_argument("--sl-pct", type=float, default=_env_float("OB_MG_SL_PCT", 0.50))
    p.add_argument("--bar-sec", type=float, default=_env_float("OB_MG_BAR_SEC", 15.0))
    p.add_argument("--sample-sec", type=float, default=_env_float("OB_MG_SAMPLE_SEC", 1.0))
    p.add_argument("--band-pct", type=float, default=1.0)
    p.add_argument("--depth-limit", type=int, default=50)
    p.add_argument("--imb-long", type=float, default=0.55)
    p.add_argument("--imb-short", type=float, default=0.45)
    p.add_argument("--momentum-min-pct", type=float, default=0.01)
    p.add_argument("--cooldown-sec", type=float, default=30.0)
    p.add_argument("--sweep", action="store_true", default=_env_bool("OB_MG_SWEEP", False),
                   help="Re-place filled grid levels one step further")
    p.add_argument("--no-sweep", action="store_true", help="Disable sweep")
    p.add_argument("--once", action="store_true", help="Exit after first signal cycle")
    p.add_argument("--flatten", action="store_true",
                   help="Cancel our grid/exits and market-close any position, then exit")
    return p


def flatten_and_exit(args: argparse.Namespace) -> int:
    load_env_file(args.env_file)
    sym = args.symbol.upper()
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys{RESET}")
        return 1
    hedge = _resolve_hedge(args, api, sec)
    n_g = cancel_our_grid(sym, api, sec, args.recv_window)
    n_e = cancel_our_exits(sym, api, sec, args.recv_window)
    print(f"Cancelled grid={n_g} exits={n_e}")
    for is_long in (True, False):
        qty, _ = get_position(sym, is_long, hedge, api, sec, args.recv_window)
        if qty > 0:
            filt = load_symbol_filters(sym)
            market_close_position(sym, is_long, qty, hedge, filt, api, sec, args.recv_window)
            print(f"{GREEN}Flattened {'LONG' if is_long else 'SHORT'} {qty:g}{RESET}")
            append_journal(sym, f"FLATTEN {'LONG' if is_long else 'SHORT'} qty={qty:g}")
    return 0


def main() -> int:
    load_env_file("")
    args = build_arg_parser().parse_args()
    if args.no_sweep:
        args.sweep = False
    if not args.execute and not args.dry_run:
        args.dry_run = True
    if args.flatten:
        return flatten_and_exit(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
