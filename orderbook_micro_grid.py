#!/usr/bin/env python3
"""15s micro-grid scalp: place FULL grid + exchange TP/SL at open.

Default grid mode is **Fibonacci** (1m swing impulse → retrace levels).
Geometric ``--grid-mode step`` remains available as fallback.

Fib entry (default):
  1. Detect swing on fib TF (default 1m)
  2. Arm only while mark is between Fib 0.000 (extreme) and Fib 0.236
  3. Place FULL LIMIT grid on retraces toward ORIGIN (no chase / no ext past 1.0)
  4. On first fill → arm TP at swing extreme + SL beyond origin
  5. If no fill before timeout / through origin → disarm and wait
  6. When all ``--levels`` are filled and position is in profit → replace SL
     with TRAILING_STOP_MARKET (default on) so a pullback does not exit at a loss
  7. After flat → cooldown 1h (``--cooldown-sec``) before next arm

Wrappers: ``./fib`` · ``./obmicro-grid`` · Telegram ``/fib`` ``/stop`` (see OBMICRO_GRID_COMMANDS.md).

Usage:
  fib LDOUSDT
  fib LDOUSDT short --entry-usdt 50
  ./obmicro-grid LDOUSDT --dry-run
  ./obmicro-grid LDOUSDT --direction long --fib-interval 5m

Env (optional):
  OB_MG_GRID_MODE=fib  OB_MG_FIB_INTERVAL=1m  OB_MG_FIB_MIN_RANGE=0.40
  OB_MG_ARM_MAX_FIB=0.236
  OB_MG_FVG_MIN_PCT=0.08  OB_MG_REQUIRE_FVG=0
  OB_MG_WAIT_PULLBACK=1  OB_MG_ARM_TIMEOUT_SEC=900
  OB_MG_RAISE_TOP=1  OB_MG_RAISE_MIN_PCT=0.05
  OB_MG_TP_MODE=avg  OB_MG_TP_PCT=0.35
  OB_MG_PROTECT_TRAIL=1  OB_MG_PROTECT_TRAIL_CALLBACK=0.2
  OB_MG_COOLDOWN_SEC=3600
  OB_MG_BASE_SIZE=10  OB_MG_LEVEL_SIZE=8  OB_MG_BAR_SEC=15
  TELEGRAM_BOT_TOKEN=  TELEGRAM_CHAT_ID=
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
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
    get_max_leverage,
    get_position,
    get_position_meta,
    get_symbol_leverage,
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


def _tg():
    """Return telegram_notify if configured; else None (no-op)."""
    try:
        import telegram_notify as tg
        if tg.is_configured():
            return tg
    except Exception:
        pass
    return None


def fib_pid_path(symbol: str) -> Path:
    d = ROOT / ".run" / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"fib-{symbol.upper()}.pid"


def register_fib_pidfile(symbol: str) -> Path:
    """Write pidfile so Telegram /stop SYMBOL can find this process."""
    path = fib_pid_path(symbol)
    path.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup() -> None:
        try:
            if path.exists() and path.read_text().strip() == str(os.getpid()):
                path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_cleanup)

    def _on_term(signum: int, frame: object) -> None:  # noqa: ARG001
        _cleanup()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass
    return path


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


def ensure_symbol_leverage(
    symbol: str,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> int:
    """Set symbol leverage to max (default) or ``--set-leverage``. Returns lev used."""
    target = 0
    set_lev = int(getattr(args, "set_leverage", 0) or 0)
    no_max = bool(getattr(args, "no_max_leverage", False))
    if set_lev > 0:
        target = set_lev
    elif not no_max:
        try:
            target = int(get_max_leverage(symbol, api, sec, recv))
        except Exception as exc:
            print(f"{YELLOW}Could not read max leverage: {exc}{RESET}")
            try:
                return int(get_symbol_leverage(symbol, api, sec, recv))
            except Exception:
                return 0
    if target <= 0:
        try:
            return int(get_symbol_leverage(symbol, api, sec, recv))
        except Exception:
            return 0
    try:
        _signed_request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol.upper(), "leverage": target},
            api,
            sec,
            recv,
        )
        print(f"{DIM}Leverage set to {target}x (max for symbol){RESET}")
        return target
    except Exception as exc:
        print(f"{YELLOW}Set leverage {target}x failed: {exc}{RESET}")
        try:
            return int(get_symbol_leverage(symbol, api, sec, recv))
        except Exception:
            return target


def apply_entry_sizing(args: argparse.Namespace) -> None:
    """Apply ``--entry-usdt`` as base notional; scale default level size with it."""
    entry = float(getattr(args, "entry_usdt", 0) or 0)
    if entry <= 0:
        return
    # Detect whether level-size is still the stock default (8) while base was 10
    level_was_default = abs(float(args.level_size) - 8.0) < 1e-9
    base_was_default = abs(float(args.base_size) - 10.0) < 1e-9
    args.base_size = entry
    if level_was_default and base_was_default:
        args.level_size = entry * 0.8
    print(
        f"{DIM}Entry sizing: base {args.base_size:g}U · level {args.level_size:g}U "
        f"(notional; margin ≈ notional ÷ leverage){RESET}"
    )


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


# Fib ladder toward impulse origin (1.0). No extensions past origin.
FIB_RETRACES = (0.236, 0.382, 0.5, 0.618, 0.786, 0.886, 1.0)
FIB_ORIGIN = 1.0
# Arm window: mark between Fib 0.000 (extreme) and this depth (default 0.236).
FIB_ARM_MAX = 0.236
FIB_TP_EXT = 1.272  # extension beyond impulse end (legacy / swing-mode helper)
FIB_SL_BUF = 0.15   # % beyond swing origin
FVG_MIN_PCT = 0.08  # min FVG height % (1m majors rarely clear 0.25%)
# TP after fills: avg = from live average (+ tp_pct); swing = fixed at impulse extreme
TP_MODE_DEFAULT = "avg"


def _fib_px(swing: SwingImpulse, ratio: float, *, is_long: bool) -> float:
    if is_long:
        return swing.high - swing.span * ratio
    return swing.low + swing.span * ratio


def pullback_depth(swing: SwingImpulse, mark: float, *, is_long: bool) -> float:
    """0 = at impulse extreme (TP side), 1 = at origin. >1 = through origin."""
    if swing.span <= 0 or mark <= 0:
        return 0.0
    if is_long:
        return (swing.high - mark) / swing.span
    return (mark - swing.low) / swing.span


def fib_levels_with_room(
    swing: SwingImpulse,
    mark: float,
    *,
    is_long: bool,
    need: int = 1,
) -> int:
    """How many Fib levels (through origin) sit on the pullback side of ``mark``."""
    if mark <= 0 or swing.span <= 0:
        return 0
    n = 0
    for r in FIB_RETRACES:
        px = _fib_px(swing, r, is_long=is_long)
        if is_long and px < mark:
            n += 1
        elif (not is_long) and px > mark:
            n += 1
        if n >= need:
            return n
    return n


def mark_through_origin(swing: SwingImpulse, mark: float, *, is_long: bool) -> bool:
    """True when price already broke past the impulse origin."""
    if mark <= 0:
        return True
    if is_long:
        return mark < swing.low
    return mark > swing.high


def resolve_tp_price(
    is_long: bool,
    entry_avg: float,
    plan: GridPlan | None,
    *,
    tp_mode: str,
    tp_pct: float,
    first_fill: bool = False,
) -> float:
    """Compute live TP.

    - ``swing``: always impulse extreme.
    - ``avg`` (default): first fill stays at swing max; later compensations use
      average ± tp_pct (capped by swing extreme so TP does not stay too far).
    """
    mode = (tp_mode or TP_MODE_DEFAULT).strip().lower()
    swing_tp = 0.0
    if plan is not None:
        swing_tp = float(plan.tp_price)
        if plan.swing is not None:
            swing_tp = plan.swing.high if is_long else plan.swing.low

    if mode in ("swing", "high", "max"):
        return swing_tp if swing_tp > 0 else (
            entry_avg * (1 + tp_pct / 100) if is_long else entry_avg * (1 - tp_pct / 100)
        )

    # avg mode: first fill = punto máximo; compensations pull TP toward avg
    if first_fill and swing_tp > 0:
        return swing_tp

    if entry_avg <= 0:
        return swing_tp
    if is_long:
        tp = entry_avg * (1 + max(tp_pct, 0.05) / 100)
        if swing_tp > 0:
            tp = min(tp, swing_tp)
        return tp
    tp = entry_avg * (1 - max(tp_pct, 0.05) / 100)
    if swing_tp > 0:
        tp = max(tp, swing_tp)
    return tp


def detect_swing_impulse(
    symbol: str,
    *,
    prefer_long: bool | None,
    interval: str = "5m",
    lookback: int = 40,
    min_range_pct: float = 0.40,
    max_span_bars: int = 12,
    mark: float | None = None,
    min_fib_room: int = 1,
    base: str = FAPI_BASE,
) -> SwingImpulse | None:
    """Find the strongest recent impulse on ``interval`` klines (drop forming bar).

    When ``mark`` is set, prefer swings that still leave ≥ ``min_fib_room``
    Fib/extension levels on the pullback side of price (avoids dead ladders).
    """
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
    # Fallback without room filter (last resort)
    best_bull_any: tuple[float, SwingImpulse] | None = None
    best_bear_any: tuple[float, SwingImpulse] | None = None

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
                if best_bull_any is None or score > best_bull_any[0]:
                    best_bull_any = (score, cand)
                room_ok = True
                if mark is not None and mark > 0:
                    if mark_through_origin(cand, mark, is_long=True):
                        room_ok = False
                    else:
                        room = fib_levels_with_room(
                            cand, mark, is_long=True, need=min_fib_room
                        )
                        room_ok = room >= min_fib_room
                        depth = pullback_depth(cand, mark, is_long=True)
                        # Prefer mark still in arm window (≤ Fib 0.236 from extreme)
                        if 0.0 <= depth <= FIB_ARM_MAX:
                            score *= 1.0 + (1.0 - depth / max(FIB_ARM_MAX, 1e-9)) * 0.4
                        elif depth > FIB_ARM_MAX:
                            score *= 0.45  # already deeper than arm window
                if room_ok and (best_bull is None or score > best_bull[0]):
                    best_bull = (score, cand)
            if hi_i < lo_i:
                cand = SwingImpulse(
                    is_long=False, low=lo, high=hi, range_pct=rng,
                    start_i=hi_i, end_i=lo_i, interval=interval,
                )
                if best_bear_any is None or score > best_bear_any[0]:
                    best_bear_any = (score, cand)
                room_ok = True
                if mark is not None and mark > 0:
                    if mark_through_origin(cand, mark, is_long=False):
                        room_ok = False
                    else:
                        room = fib_levels_with_room(
                            cand, mark, is_long=False, need=min_fib_room
                        )
                        room_ok = room >= min_fib_room
                        depth = pullback_depth(cand, mark, is_long=False)
                        if 0.0 <= depth <= FIB_ARM_MAX:
                            score *= 1.0 + (1.0 - depth / max(FIB_ARM_MAX, 1e-9)) * 0.4
                        elif depth > FIB_ARM_MAX:
                            score *= 0.45
                if room_ok and (best_bear is None or score > best_bear[0]):
                    best_bear = (score, cand)

    bull = (best_bull or best_bull_any)
    bear = (best_bear or best_bear_any)
    bull_s = bull[1] if bull else None
    bear_s = bear[1] if bear else None
    if prefer_long is True:
        return bull_s or bear_s
    if prefer_long is False:
        return bear_s or bull_s
    if bull and bear:
        return bull_s if bull[0] >= bear[0] else bear_s
    return bull_s or bear_s


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
    """Retrace grid on swing down to origin (1.0); no levels past origin.

    Only rungs on the adverse side of ``entry``. Deepest rung is Fib 1.0
    (impulse origin) when still below/above mark.
    """
    if entry <= 0 or swing.span <= 0:
        raise ValueError("invalid entry/swing for fib grid")
    n_take = max(1, int(levels))

    rows: list[GridLevel] = []
    for r in FIB_RETRACES:
        if len(rows) >= n_take:
            break
        px = _fib_px(swing, r, is_long=is_long)
        if is_long:
            # Buy LIMITs must sit strictly below mark (incl. ORIGIN)
            if px >= entry:
                continue
        else:
            if px <= entry:
                continue
        size = base_usdt if not rows else level_usdt
        _, qty = qty_for_notional(size, px, filt)
        if qty <= 0:
            continue
        dist = (px / entry - 1.0) * 100
        name = "ORIGIN" if abs(r - FIB_ORIGIN) < 1e-9 else f"Fib {r:.3f}"
        rows.append(
            GridLevel(
                idx=len(rows) + 1,
                name=name,
                price=px,
                size_usdt=float(qty) * px,
                qty=qty,
                dist_pct=dist,
                fib_ratio=r,
            )
        )
    for i, lv in enumerate(rows, start=1):
        lv.idx = i

    if not rows:
        # No levels under mark yet — still return empty for caller to skip
        _, base_qty = qty_for_notional(base_usdt, entry, filt)
        return GridPlan(
            is_long=is_long,
            entry=entry,
            base_usdt=float(base_qty) * entry,
            base_qty=base_qty,
            levels=[],
            tp_price=swing.high if is_long else swing.low,
            sl_price=swing.low if is_long else swing.high,
            planned_qty=0.0,
            planned_avg=entry,
            mode="fib",
            note="no fib levels below/above mark yet",
            swing=swing,
            fvg=fvg,
        )

    cum_q = 0.0
    cum_n = 0.0
    for lv in rows:
        cum_q += lv.qty
        cum_n += lv.size_usdt
    planned_avg = cum_n / cum_q if cum_q > 0 else entry
    base_qty = rows[0].qty
    base_usdt_eff = rows[0].size_usdt

    # TP at swing extreme. SL beyond origin (never inside the impulse start).
    if is_long:
        tp = swing.high
        sl_swing = swing.low * (1 - sl_buf_pct / 100)
        sl_pct_px = entry * (1 - sl_pct / 100)
        sl = min(sl_swing, sl_pct_px)
        deepest = min(lv.price for lv in rows)
        sl = min(sl, deepest * (1 - sl_buf_pct / 100))
    else:
        tp = swing.low
        sl_swing = swing.high * (1 + sl_buf_pct / 100)
        sl_pct_px = entry * (1 + sl_pct / 100)
        sl = max(sl_swing, sl_pct_px)
        deepest = max(lv.price for lv in rows)
        sl = max(sl, deepest * (1 + sl_buf_pct / 100))

    note = (
        f"fib {swing.interval} swing "
        f"{price_fmt(swing.low)}→{price_fmt(swing.high)} "
        f"({swing.range_pct:.2f}%) · TP ref @ swing {'high' if is_long else 'low'} "
        f"· arm window Fib 0–{FIB_ARM_MAX:g}"
    )
    if fvg is not None:
        note += (
            f" · FVG {price_fmt(fvg.low)}–{price_fmt(fvg.high)} "
            f"({fvg.size_pct:.2f}%)"
        )
    return GridPlan(
        is_long=is_long,
        entry=entry,
        base_usdt=base_usdt_eff,
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
    *,
    ignore_arm_window: bool = False,
) -> GridPlan | None:
    """Build Fib (default) or step grid.

    Fib mode requires a swing. FVG ≥ ``fvg_min_pct`` is preferred; with
    ``--require-fvg`` a missing aligned FVG skips the cycle.
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
                mark=entry,
                min_fib_room=max(1, min(2, int(args.levels))),
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

        # Arm only while mark is between Fib 0.000 and Fib arm_max (default 0.236)
        if not ignore_arm_window:
            arm_max = float(getattr(args, "arm_max_fib", FIB_ARM_MAX))
            if is_long and entry > swing.high:
                print(f"{YELLOW}Mark above swing high — no chase — skip{RESET}")
                return None
            if (not is_long) and entry < swing.low:
                print(f"{YELLOW}Mark below swing low — no chase — skip{RESET}")
                return None
            if mark_through_origin(swing, entry, is_long=is_long):
                print(
                    f"{YELLOW}Mark through origin "
                    f"({price_fmt(swing.low if is_long else swing.high)}) — skip{RESET}"
                )
                return None
            depth = pullback_depth(swing, entry, is_long=is_long)
            if depth < 0:
                print(f"{YELLOW}Mark beyond swing extreme — skip{RESET}")
                return None
            if depth > arm_max:
                print(
                    f"{YELLOW}Mark past arm window "
                    f"(depth {depth:.2f} > Fib {arm_max:g}; need between 0–{arm_max:g}) — skip{RESET}"
                )
                return None

        fvg: FvgZone | None = None
        require_fvg = bool(getattr(args, "require_fvg", False))
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
        else:
            print(f"{DIM}No aligned FVG ≥{min_fvg:g}% — Fib-only{RESET}")

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
        if not plan.levels and not ignore_arm_window:
            print(
                f"{YELLOW}No Fib levels below/above mark toward origin — skip{RESET}"
            )
            return None
        # Full grid: need enough LIMITs still under/above mark (arm window ⇒ most fit)
        if not ignore_arm_window:
            want = max(1, int(args.levels))
            if len(plan.levels) < want:
                print(
                    f"{YELLOW}Incomplete grid ({len(plan.levels)}/{want} LIMITs) "
                    f"— mark not high enough in window — skip{RESET}"
                )
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


def render_plan(symbol: str, plan: GridPlan, *, include_ladder: bool = True) -> str:
    side = "LONG" if plan.is_long else "SHORT"
    color = GREEN if plan.is_long else RED
    mode = plan.mode.upper()
    lines = [
        f"{BOLD}{CYAN}Micro-grid · {symbol.upper()} · {color}{side}{RESET}  {DIM}[{mode}]{RESET}",
        f"{DIM}mark {price_fmt(plan.entry)}  ·  "
        f"{len(plan.levels)} LIMIT levels  ·  "
        f"TP ref {price_fmt(plan.tp_price)} (swing)  SL {price_fmt(plan.sl_price)}  ·  "
        f"planned {plan.planned_qty:g} qty · avg {price_fmt(plan.planned_avg)}{RESET}",
    ]
    if plan.note:
        lines.append(f"{DIM}{plan.note}{RESET}")
    lines.append(
        f"{DIM}TP: 1st fill @ swing max · later fills avg+tp% (or --tp-mode swing = always max){RESET}"
    )
    if include_ladder:
        lines.extend(
            render_ladder_lines(
                plan,
                mark=plan.entry,
                tp_price=plan.tp_price,
                sl_price=plan.sl_price,
            )
        )
        if plan.mode == "fib" and plan.swing is not None:
            lines.append(f"{DIM}(LIMIT only on GRID rows · TP/SL armed after first fill){RESET}")
    return "\n".join(lines)


def render_ladder_lines(
    plan: GridPlan,
    *,
    mark: float,
    tp_price: float | None = None,
    sl_price: float | None = None,
    entry: float | None = None,
    qty: float | None = None,
    upnl: float | None = None,
    status: str = "",
    open_level_idxs: set[int] | None = None,
    filled_level_idxs: set[int] | None = None,
    trail_armed: bool = False,
    trail_callback: float = 0.2,
) -> list[str]:
    """Fib/step ladder with MARK (and optional ENTRY) inserted in price order."""
    tp_px = float(tp_price if tp_price is not None else plan.tp_price)
    sl_px = float(sl_price if sl_price is not None else plan.sl_price)
    open_idxs = open_level_idxs or set()
    filled_idxs = filled_level_idxs or set()

    if plan.mode == "fib" and plan.swing is not None:
        swing = plan.swing
        limit_by_r = {
            round(lv.fib_ratio, 3): lv
            for lv in plan.levels
            if lv.fib_ratio is not None
        }
        ladder_ratios = (0.0, *FIB_RETRACES)
        lines = [
            "",
            f"{'LEVEL':<14} {'ROLE':<10} {'PRICE':>14} {'Δ mark':>9} {'USDT':>18}",
            f"{DIM}{'-' * 70}{RESET}",
        ]
        # (price, label, role, lim_text, kind)
        rows_out: list[tuple[float, str, str, str, str]] = []
        for r in ladder_ratios:
            if plan.is_long:
                px = swing.high - swing.span * r
            else:
                px = swing.low + swing.span * r
            if r <= 0.0:
                role, label = "TP", "Fib 0.000"
            elif r >= 1.0:
                role, label = "ORIGIN", "Fib 1.000"
            else:
                role, label = "GRID", f"Fib {r:.3f}"
            lv = limit_by_r.get(round(r, 3))
            if lv is not None:
                usdt = f"{lv.size_usdt:.2f}U"
                if lv.idx in filled_idxs:
                    role = "FILLED"
                    lim = f"{usdt} ✓"
                    px = lv.price
                elif lv.idx in open_idxs:
                    role = "OPEN"
                    lim = usdt
                    px = lv.price
                else:
                    # Planned rung (not yet synced / not placed)
                    role = "LIMIT"
                    lim = usdt
                    px = lv.price
            elif r <= 0.0:
                lim = "algo"
                px = tp_px
            else:
                lim = "—"
            rows_out.append((px, label, role, lim, "level"))

        if trail_armed:
            # Show trail at entry (BE floor); Binance trails from mark extreme.
            trail_px = float(entry) if entry and entry > 0 else sl_px
            rows_out.append(
                (trail_px, "TRAIL", "TRAIL", f"cb={trail_callback:g}%", "level")
            )
        else:
            rows_out.append((sl_px, "SL", "SL", "algo", "level"))

        if entry is not None and entry > 0:
            if qty and qty > 0:
                ent_lim = f"{qty * entry:.2f}U"
            else:
                ent_lim = ""
            rows_out.append((entry, "▶ ENTRY", "avg", ent_lim, "entry"))

        mark_lim = ""
        if upnl is not None:
            mark_lim = f"uPnL={upnl:+.4f}"
        elif status:
            mark_lim = status
        rows_out.append((mark, "▶ MARK", "live", mark_lim, "mark"))

        rows_out.sort(key=lambda x: x[0], reverse=plan.is_long)

        for px, label, role, lim, kind in rows_out:
            dist = (px / mark - 1.0) * 100 if mark > 0 else 0.0
            if kind == "mark":
                lines.append(
                    f"{CYAN}{label:<14} {role:<10} {price_fmt(px):>14} "
                    f"{'0.00%':>9} {lim:>18}{RESET}"
                )
            elif kind == "entry":
                lines.append(
                    f"{YELLOW}{label:<14} {role:<10} {price_fmt(px):>14} "
                    f"{dist:>+8.2f}% {lim:>18}{RESET}"
                )
            else:
                if role == "FILLED":
                    role_c = GREEN
                elif role == "OPEN":
                    role_c = YELLOW
                elif role == "TP":
                    role_c = GREEN
                elif role == "SL":
                    role_c = RED
                elif role == "TRAIL":
                    role_c = CYAN
                elif role == "LIMIT":
                    role_c = YELLOW
                else:
                    role_c = DIM
                lines.append(
                    f"{label:<14} {role_c}{role:<10}{RESET} {price_fmt(px):>14} "
                    f"{dist:>+8.2f}% {lim:>18}"
                )
        if plan.fvg is not None:
            lines.append(
                f"{DIM}FVG zone {price_fmt(plan.fvg.low)}–{price_fmt(plan.fvg.high)} "
                f"({plan.fvg.size_pct:.2f}%){RESET}"
            )
        return lines

    # step mode
    lines = [
        "",
        f"{'ORDER':<12} {'ROLE':>8} {'PRICE':>14} {'Δ%':>8} {'USDT':>10}",
        f"{DIM}{'-' * 60}{RESET}",
    ]
    for lv in plan.levels:
        if lv.idx in filled_idxs:
            role = "FILLED"
        elif lv.idx in open_idxs:
            role = "OPEN"
        else:
            role = "LIMIT"
        lines.append(
            f"{lv.name:<12} {role:>8} {price_fmt(lv.price):>14} "
            f"{lv.dist_pct:>+7.2f}% {lv.size_usdt:>9.2f}U"
        )
    lines.append(
        f"{CYAN}{'▶ MARK':<12} {'live':>8} {price_fmt(mark):>14} "
        f"{'0.00%':>8} {'':>10}{RESET}"
    )
    return lines


def sync_grid_level_status(
    symbol: str,
    state: "CycleState",
    api: str,
    sec: str,
    recv: int,
) -> tuple[set[int], set[int]]:
    """Return (open_idxs, filled_idxs) from live exchange open orders."""
    if state.plan is None:
        return set(), set()
    open_cids = {
        _order_cid(o)
        for o in list_open_orders(symbol, api, sec, recv)
        if _order_cid(o).startswith(GRID_PREFIX)
    }
    open_idxs: set[int] = set()
    filled_idxs: set[int] = set()
    for lv in state.plan.levels:
        cid = grid_cid(symbol, lv.idx)
        if cid in open_cids:
            open_idxs.add(lv.idx)
            state.filled_levels.discard(lv.idx)
        else:
            # Missing from book: filled (or cancelled). Treat as filled once armed/active.
            if state.active or state.pending or lv.idx in state.filled_levels:
                filled_idxs.add(lv.idx)
                state.filled_levels.add(lv.idx)
    return open_idxs, filled_idxs


def render_live_table(
    symbol: str,
    state: "CycleState",
    mark: float,
    *,
    qty: float = 0.0,
    entry: float = 0.0,
    upnl: float | None = None,
    open_limits: int | None = None,
    age_sec: float | None = None,
    open_level_idxs: set[int] | None = None,
    filled_level_idxs: set[int] | None = None,
) -> list[str]:
    """Live ladder (same columns) with MARK/ENTRY in price order."""
    plan = state.plan
    if plan is None:
        return []
    side = "LONG" if state.is_long else "SHORT"
    color = GREEN if state.is_long else RED
    n_open = len(open_level_idxs) if open_level_idxs is not None else open_limits
    n_fill = len(filled_level_idxs) if filled_level_idxs is not None else 0
    if state.pending:
        head = (
            f"{DIM}PENDING {color}{side}{RESET}{DIM} · {symbol.upper()} · "
            f"mid {price_fmt(mark)} · open={n_open if n_open is not None else '?'} "
            f"filled={n_fill} · age={age_sec:.0f}s{RESET}"
            if age_sec is not None else
            f"{DIM}PENDING {color}{side}{RESET}{DIM} · {symbol.upper()} · mid {price_fmt(mark)}{RESET}"
        )
        status = f"open={n_open}" if n_open is not None else "wait"
        body = render_ladder_lines(
            plan, mark=mark, tp_price=state.tp, sl_price=state.sl, status=status,
            open_level_idxs=open_level_idxs, filled_level_idxs=filled_level_idxs,
            trail_armed=state.trail_armed,
        )
    else:
        notional = (qty * entry) if qty > 0 and entry > 0 else 0.0
        head = (
            f"{color}{side}{RESET} {symbol.upper()} · {notional:.2f}U · "
            f"entry {price_fmt(entry)} · mid {price_fmt(mark)}"
            + (f" · uPnL={upnl:+.4f}" if upnl is not None else "")
            + (f" · filled={n_fill} open={n_open if n_open is not None else 0}" if plan.levels else "")
            + (f" · {CYAN}TRAIL{RESET}" if state.trail_armed else "")
        )
        body = render_ladder_lines(
            plan,
            mark=mark,
            tp_price=state.tp or plan.tp_price,
            sl_price=state.sl or plan.sl_price,
            entry=entry if entry > 0 else None,
            qty=qty if qty > 0 else None,
            upnl=upnl,
            open_level_idxs=open_level_idxs,
            filled_level_idxs=filled_level_idxs,
            trail_armed=state.trail_armed,
            trail_callback=float(state.trail_callback or 0.2),
        )
    return [head, *body]


def print_live_table(state: "CycleState", lines: list[str], *, force: bool = False) -> None:
    """Refresh live ladder in-place (overwrite previous block)."""
    if not lines:
        return
    out = sys.stdout
    now = time.time()
    if not out.isatty():
        # Log files / pipes: avoid spam — refresh at most every 10s
        last = float(getattr(state, "ui_last_print", 0.0) or 0.0)
        if not force and now - last < 10.0:
            return
        state.ui_last_print = now
        print()
        for line in lines:
            print(line)
        state.ui_lines = 0
        return

    prev = int(getattr(state, "ui_lines", 0) or 0)
    if prev > 0:
        out.write(f"\033[{prev}A")
        for _ in range(prev):
            out.write("\033[2K\n")
        out.write(f"\033[{prev}A")
    for line in lines:
        print(line)
    state.ui_lines = len(lines)
    state.ui_last_print = now
    out.flush()


def clear_live_table(state: "CycleState") -> None:
    prev = int(getattr(state, "ui_lines", 0) or 0)
    if prev > 0 and sys.stdout.isatty():
        sys.stdout.write(f"\033[{prev}A")
        for _ in range(prev):
            sys.stdout.write("\033[2K\n")
        sys.stdout.write(f"\033[{prev}A")
        sys.stdout.flush()
    state.ui_lines = 0


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


def _algo_order_type(o: dict) -> str:
    return str(o.get("orderType") or o.get("type") or "").upper()


def cancel_our_sl(symbol: str, api: str, sec: str, recv: int) -> int:
    """Cancel fixed STOP_MARKET SL algos (keep TP / trailing)."""
    sym = symbol.upper()
    killed = 0
    for o in list_open_algos(symbol, api, sec, recv):
        cid = _algo_cid_of(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        otype = _algo_order_type(o)
        if "TRAILING" in otype:
            continue
        if "TAKE_PROFIT" in otype or "TP" in cid:
            continue
        if "STOP" not in otype and "SL" not in cid:
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


def cancel_our_trailing(symbol: str, api: str, sec: str, recv: int) -> int:
    sym = symbol.upper()
    killed = 0
    for o in list_open_algos(symbol, api, sec, recv):
        cid = _algo_cid_of(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        if "TRAILING" not in _algo_order_type(o) and "TR" not in cid:
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


def find_our_trailing(symbol: str, api: str, sec: str, recv: int) -> dict | None:
    sym = symbol.upper()
    for o in list_open_algos(symbol, api, sec, recv):
        cid = _algo_cid_of(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        if "TRAILING" in _algo_order_type(o) or cid.startswith(f"{ALGO_PREFIX}TR"):
            return o
    return None


def grid_levels_complete(
    state: "CycleState",
    open_idxs: set[int],
    filled_idxs: set[int],
) -> bool:
    """True when every placed ``--levels`` rung is filled and none remain open."""
    if state.plan is None or not state.plan.levels:
        return False
    needed = {lv.idx for lv in state.plan.levels}
    return bool(needed) and not open_idxs and needed <= filled_idxs


def mark_profit_pct(is_long: bool, entry: float, mark: float) -> float:
    """Gross mark vs entry %% (positive = in profit)."""
    if entry <= 0 or mark <= 0:
        return 0.0
    raw = (mark / entry - 1.0) * 100.0
    return raw if is_long else -raw


def place_protect_trailing(
    symbol: str,
    is_long: bool,
    qty: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    callback: float,
) -> dict:
    """Replace fixed SL with reduce-only TRAILING_STOP_MARKET (activates immediately)."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    qty_d = _round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        raise ValueError("qty too small for trailing")
    qty_str = f"{qty_d:.{qty_dp}f}"
    cb = max(0.1, min(10.0, float(callback)))
    close_side = "SELL" if is_long else "BUY"

    cancel_our_sl(symbol, api, sec, recv)
    cancel_our_trailing(symbol, api, sec, recv)

    params: dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": close_side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": qty_str,
        "callbackRate": cb,
        "workingType": "CONTRACT_PRICE",
        "clientAlgoId": algo_cid("TR", symbol),
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def maybe_arm_full_fill_trail(
    symbol: str,
    state: "CycleState",
    mark: float,
    qty: float,
    entry: float,
    upnl: float,
    open_idxs: set[int],
    filled_idxs: set[int],
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> bool:
    """After all grid levels fill, arm trailing once mark profit covers the callback.

    Returns True if trailing was newly armed (or refreshed).
    """
    if not bool(getattr(args, "protect_trail", True)):
        return False
    if not state.active or state.pending or state.trail_armed:
        return False
    if qty <= 0 or entry <= 0:
        return False
    if not grid_levels_complete(state, open_idxs, filled_idxs):
        return False

    callback = float(getattr(args, "protect_trail_callback", 0.2) or 0.2)
    # Need enough green that a callback% pullback from mark stays ~flat (no loss).
    min_pct = max(
        float(getattr(args, "protect_arm_pnl_pct", 0.0) or 0.0),
        callback,
    )
    profit = mark_profit_pct(state.is_long, entry, mark)
    if upnl < 0 or profit < min_pct:
        return False

    # Already have our trailing on the book (e.g. restart) — adopt it.
    existing = find_our_trailing(symbol, api, sec, recv)
    if existing is not None:
        state.trail_armed = True
        state.trail_callback = callback
        try:
            state.trail_callback = float(existing.get("callbackRate") or callback)
        except (TypeError, ValueError):
            pass
        state.sl = entry
        return False

    clear_live_table(state)
    try:
        resp = place_protect_trailing(
            symbol, state.is_long, qty, filt, hedge, api, sec, recv, callback,
        )
    except Exception as exc:
        print(f"{RED}Protect trail failed: {exc}{RESET}")
        append_journal(symbol, f"ERROR protect_trail {exc}")
        return False

    state.trail_armed = True
    state.trail_callback = callback
    state.sl = entry  # display: trail protects toward avg
    print(
        f"{GREEN}✓ Full grid filled · profit {profit:+.2f}% — "
        f"SL → TRAILING cb={callback:g}% (algoId={resp.get('algoId')}){RESET}"
    )
    append_journal(
        symbol,
        f"PROTECT_TRAIL cb={callback:g} profit={profit:.3f}% qty={qty:g} "
        f"entry={entry:.8g} mark={mark:.8g} upnl={upnl:.4f}",
    )
    tg = _tg()
    if tg is not None:
        tg.notify_fib_protect_trail(
            symbol, "LONG" if state.is_long else "SHORT",
            qty, entry, callback, profit_pct=profit, pnl_usdt=upnl,
        )
    return True


def read_our_exit_triggers(
    symbol: str,
    api: str,
    sec: str,
    recv: int,
) -> tuple[float, float]:
    """Return (tp, sl) trigger prices from our open algo orders (0 if missing)."""
    sym = symbol.upper()
    tp = 0.0
    sl = 0.0
    for o in list_open_algos(symbol, api, sec, recv):
        cid = _algo_cid_of(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        trig = float(o.get("triggerPrice") or 0)
        if trig <= 0:
            continue
        otype = str(o.get("orderType") or o.get("type") or "").upper()
        if "TRAILING" in otype:
            continue
        if "TAKE_PROFIT" in otype or "TP" in cid:
            tp = trig
        elif "STOP" in otype or "SL" in cid:
            sl = trig
    return tp, sl


def adopt_existing_position(
    symbol: str,
    is_long: bool,
    mark: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> CycleState | None:
    """Rebuild cycle state + live table from an already-open same-side position."""
    qty, entry = get_position(symbol, is_long, hedge, api, sec, recv)
    if qty <= 0 or entry <= 0:
        return None

    plan = build_grid_plan(
        symbol, mark, is_long, args, filt, ignore_arm_window=True,
    )
    if plan is None:
        plan = build_step_grid(
            entry,
            is_long,
            levels=max(1, int(args.levels)),
            step_pct=args.step_pct,
            base_usdt=args.base_size,
            level_usdt=args.level_size,
            tp_pct=args.tp_pct,
            sl_pct=args.sl_pct,
            filt=filt,
        )
    plan.note = ((plan.note + " · ") if plan.note else "") + "adopted"

    tp, sl = read_our_exit_triggers(symbol, api, sec, recv)
    trail = find_our_trailing(symbol, api, sec, recv)
    trail_armed = trail is not None
    trail_cb = float(getattr(args, "protect_trail_callback", 0.2) or 0.2)
    if trail is not None:
        try:
            trail_cb = float(trail.get("callbackRate") or trail_cb)
        except (TypeError, ValueError):
            pass
        cancel_our_sl(symbol, api, sec, recv)  # trail replaces fixed SL
    exits_armed = (tp > 0 and sl > 0) or trail_armed
    if not exits_armed:
        tp_mode = str(getattr(args, "tp_mode", TP_MODE_DEFAULT))
        tp = resolve_tp_price(
            is_long, entry, plan, tp_mode=tp_mode,
            tp_pct=float(getattr(args, "tp_pct", 0.35)), first_fill=True,
        )
        sl = float(plan.sl_price)
        try:
            tp, sl = place_exchange_exits(
                symbol, is_long, qty, entry, mark or entry,
                tp, sl, filt, hedge, api, sec, recv,
            )
            exits_armed = True
            append_journal(
                symbol,
                f"ADOPT_EXITS tp={tp:.8g} sl={sl:.8g} qty={qty:g}",
            )
        except Exception as exc:
            print(f"{YELLOW}Adopt: could not arm exits ({exc}){RESET}")
            tp = tp or float(plan.tp_price)
            sl = sl or float(plan.sl_price)

    side = "LONG" if is_long else "SHORT"
    print(
        f"{CYAN}Adopted existing {side} on {symbol.upper()} · "
        f"qty={qty:g} @ {price_fmt(entry)} · rebuilding table{RESET}"
    )
    append_journal(
        symbol,
        f"ADOPT {side} qty={qty:g} entry={entry:.8g} tp={tp:.8g} sl={sl:.8g}"
        f"{' trail' if trail_armed else ''}",
    )

    meta = get_position_meta(symbol, is_long, hedge, api, sec, recv)
    upnl = float(meta.get("unrealized_pnl") or 0)
    notional = float(meta.get("notional") or (qty * entry))
    lev = int(meta.get("leverage") or 0)
    tg = _tg()
    if tg is not None:
        tg.notify_fib_adopt(
            symbol, side, qty, entry,
            vol_usdt=notional, leverage=lev, pnl_usdt=upnl, trail=trail_armed,
        )

    state = CycleState(
        active=True,
        pending=False,
        exits_armed=exits_armed,
        trail_armed=trail_armed,
        trail_callback=trail_cb,
        is_long=is_long,
        entry=entry,
        plan=plan,
        tp=tp,
        sl=entry if trail_armed else sl,
        last_qty=qty,
        opened_at=time.time(),
        armed_at=time.time(),
        swing_high_anchor=(
            plan.swing.high if plan.swing and is_long else (
                plan.swing.low if plan.swing else 0.0
            )
        ),
        last_notional=notional,
        last_upnl=upnl,
        last_leverage=lev,
    )
    open_idxs, filled_idxs = sync_grid_level_status(symbol, state, api, sec, recv)
    print_live_table(
        state,
        render_live_table(
            symbol, state, mark,
            qty=qty, entry=entry, upnl=upnl,
            open_level_idxs=open_idxs,
            filled_level_idxs=filled_idxs,
        ),
        force=True,
    )
    return state


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
    active: bool = False       # position open
    pending: bool = False      # grid LIMITs armed, waiting for pullback fill
    exits_armed: bool = False
    trail_armed: bool = False  # full-grid protect trailing replaced fixed SL
    trail_callback: float = 0.2
    is_long: bool = True
    entry: float = 0.0
    plan: GridPlan | None = None
    tp: float = 0.0
    sl: float = 0.0
    last_qty: float = 0.0
    filled_levels: set[int] = field(default_factory=set)
    opened_at: float = 0.0
    armed_at: float = 0.0
    last_swing_check_at: float = 0.0
    swing_high_anchor: float = 0.0  # last raised top (long) / bottom (short)
    ui_lines: int = 0  # live table rows currently on screen (for in-place refresh)
    ui_last_print: float = 0.0
    last_notional: float = 0.0
    last_upnl: float = 0.0
    last_leverage: int = 0


def arm_cycle(
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
    wait_pullback: bool,
    args: argparse.Namespace | None = None,
) -> CycleState:
    """Arm full LIMIT grid (+ optional market base). TP/SL only after first fill when waiting."""
    # Wait-pullback: header only — live ladder is the single table (avoids duplicate).
    print(render_plan(symbol, plan, include_ladder=not wait_pullback))
    append_journal(
        symbol,
        f"PLAN {plan.mode} {'LONG' if plan.is_long else 'SHORT'} mark={plan.entry:.8g} "
        f"levels={len(plan.levels)} tp={plan.tp_price:.8g} sl={plan.sl_price:.8g} "
        f"wait={int(wait_pullback)} {plan.note}",
    )

    if dry_run:
        print(f"{YELLOW}Dry-run — no orders sent{RESET}")
        return CycleState(active=False, pending=False)

    if not plan.levels and wait_pullback:
        print(f"{YELLOW}No LIMIT levels to arm{RESET}")
        return CycleState(active=False, pending=False)

    # Cancel any prior leftover
    cancel_our_grid(symbol, api, sec, recv)
    cancel_our_exits(symbol, api, sec, recv)

    if wait_pullback:
        n = place_grid_limits(symbol, plan, filt, hedge, api, sec, recv)
        append_journal(symbol, f"ARM grid={n}/{len(plan.levels)} wait_pullback")
        if n < len(plan.levels):
            print(f"{YELLOW}Armed {n}/{len(plan.levels)} LIMITs (some failed){RESET}")
        state = CycleState(
            active=False,
            pending=True,
            exits_armed=False,
            is_long=plan.is_long,
            entry=0.0,
            plan=plan,
            tp=plan.tp_price,
            sl=plan.sl_price,
            last_qty=0.0,
            armed_at=time.time(),
            last_swing_check_at=time.time(),
            swing_high_anchor=plan.swing.high if plan.swing and plan.is_long else (
                plan.swing.low if plan.swing else 0.0
            ),
        )
        print_live_table(
            state,
            render_live_table(
                symbol, state, mark,
                open_limits=n, age_sec=0.0,
                open_level_idxs=sync_grid_level_status(
                    symbol, state, api, sec, recv,
                )[0],
                filled_level_idxs=set(),
            ),
            force=True,
        )
        tg = _tg()
        if tg is not None:
            grid_vol = sum(float(lv.size_usdt) for lv in plan.levels)
            tg.notify_fib_grid_armed(
                symbol, "LONG" if plan.is_long else "SHORT", n,
                wait_pullback=True, grid_vol_usdt=grid_vol, mark=mark,
            )
        return state

    # Legacy chase path: MARKET base then grid + exits
    qty_str, _ = qty_for_notional(plan.base_usdt, plan.entry, filt)
    print(f"\n{BOLD}Opening MARKET base {qty_str}…{RESET}")
    market_open(symbol, plan.is_long, qty_str, hedge, api, sec, recv)
    time.sleep(0.35)
    qty, entry = get_position(symbol, plan.is_long, hedge, api, sec, recv)
    if qty <= 0 or entry <= 0:
        entry = plan.entry
        qty = plan.base_qty
    print(f"{GREEN}✓ Base filled ~{qty:g} @ {price_fmt(entry)}{RESET}")
    append_journal(symbol, f"OPEN {'LONG' if plan.is_long else 'SHORT'} qty={qty:g} entry={entry:.8g}")

    print(f"{BOLD}Placing full grid ({len(plan.levels)} limits)…{RESET}")
    n = place_grid_limits(symbol, plan, filt, hedge, api, sec, recv)
    append_journal(symbol, f"GRID placed={n}/{len(plan.levels)}")

    print(f"{BOLD}Arming exchange TP/SL…{RESET}")
    tp_mode = str(getattr(args, "tp_mode", TP_MODE_DEFAULT)) if args else TP_MODE_DEFAULT
    tp_pct = float(getattr(args, "tp_pct", 0.35)) if args else 0.35
    tp_target = resolve_tp_price(plan.is_long, entry, plan, tp_mode=tp_mode, tp_pct=tp_pct, first_fill=True)
    tp, sl = place_exchange_exits(
        symbol, plan.is_long, qty, entry, mark or entry,
        tp_target, plan.sl_price, filt, hedge, api, sec, recv,
    )
    append_journal(symbol, f"EXITS tp={tp:.8g} sl={sl:.8g} qty={qty:g} tp_mode={tp_mode}")
    tg = _tg()
    if tg is not None:
        side = "LONG" if plan.is_long else "SHORT"
        grid_vol = sum(float(lv.size_usdt) for lv in plan.levels) + float(plan.base_usdt)
        tg.notify_fib_grid_armed(
            symbol, side, n, wait_pullback=False, grid_vol_usdt=grid_vol, mark=mark,
        )
        tg.notify_fib_open(
            symbol, side, qty, entry, tp=tp, sl=sl, vol_usdt=qty * entry,
        )
    return CycleState(
        active=True,
        pending=False,
        exits_armed=True,
        is_long=plan.is_long,
        entry=entry,
        plan=plan,
        tp=tp,
        sl=sl,
        last_qty=qty,
        opened_at=time.time(),
        armed_at=time.time(),
        last_notional=qty * entry,
    )


def disarm_pending(
    symbol: str,
    state: CycleState,
    api: str,
    sec: str,
    recv: int,
    *,
    reason: str,
) -> None:
    """Cancel unfilled grid (and exits if any) and clear pending state."""
    clear_live_table(state)
    n_g = cancel_our_grid(symbol, api, sec, recv)
    n_e = cancel_our_exits(symbol, api, sec, recv)
    print(f"{YELLOW}Disarm ({reason}) · cancelled grid={n_g} exits={n_e}{RESET}")
    append_journal(symbol, f"DISARM reason={reason} grid={n_g} exits={n_e}")
    tg = _tg()
    if tg is not None:
        tg.notify_fib_disarm(
            symbol, "LONG" if state.is_long else "SHORT", reason,
        )
    state.pending = False
    state.active = False
    state.exits_armed = False
    state.trail_armed = False
    state.plan = None
    state.last_qty = 0.0
    state.filled_levels.clear()


def _raise_check_period_sec(interval: str) -> float:
    """How often to re-check swing top while pending (scales with fib TF)."""
    raw = (interval or "5m").strip().lower()
    try:
        if raw.endswith("m"):
            return max(15.0, float(raw[:-1]) * 15.0)  # 1m→15s, 5m→75s
        if raw.endswith("s"):
            return max(10.0, float(raw[:-1]))
    except ValueError:
        pass
    return 30.0


def try_raise_pending_top(
    symbol: str,
    state: CycleState,
    mark: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> bool:
    """If impulse extends while pending, rebuild Fib + TP at the new extreme.

    Returns True if grid was re-armed.
    """
    if not state.pending or state.plan is None or state.plan.mode != "fib":
        return False
    if not bool(getattr(args, "raise_top", True)):
        return False
    old = state.plan.swing
    if old is None:
        return False

    now = time.time()
    period = _raise_check_period_sec(args.fib_interval)
    if now - state.last_swing_check_at < period:
        return False
    state.last_swing_check_at = now

    min_raise = max(0.02, float(getattr(args, "raise_min_pct", 0.05))) / 100.0
    prefer = True if state.is_long else False
    try:
        fresh = detect_swing_impulse(
            symbol,
            prefer_long=prefer,
            interval=args.fib_interval,
            lookback=args.fib_lookback,
            min_range_pct=args.fib_min_range,
            max_span_bars=args.fib_max_span,
            mark=mark,
            min_fib_room=1,
        )
    except Exception:
        fresh = None

    if state.is_long:
        cand_high = old.high
        if fresh is not None and fresh.is_long:
            cand_high = max(cand_high, fresh.high)
        cand_high = max(cand_high, mark)
        if cand_high <= old.high * (1.0 + min_raise):
            return False
        # Keep original trough when possible (deeper pullback origin)
        new_low = old.low
        if fresh is not None and fresh.is_long and fresh.low < old.low:
            new_low = fresh.low
        new_swing = SwingImpulse(
            is_long=True,
            low=new_low,
            high=cand_high,
            range_pct=(cand_high - new_low) / new_low * 100 if new_low > 0 else 0.0,
            start_i=old.start_i,
            end_i=fresh.end_i if fresh and fresh.is_long else old.end_i,
            interval=old.interval,
        )
    else:
        # Short: impulse extends lower → new trough, TP @ new low
        cand_low = old.low
        if fresh is not None and not fresh.is_long:
            cand_low = min(cand_low, fresh.low)
        cand_low = min(cand_low, mark)
        if cand_low >= old.low * (1.0 - min_raise):
            return False
        new_high = old.high
        if fresh is not None and not fresh.is_long and fresh.high > old.high:
            new_high = fresh.high
        new_swing = SwingImpulse(
            is_long=False,
            low=cand_low,
            high=new_high,
            range_pct=(new_high - cand_low) / cand_low * 100 if cand_low > 0 else 0.0,
            start_i=old.start_i,
            end_i=fresh.end_i if fresh and not fresh.is_long else old.end_i,
            interval=old.interval,
        )

    new_plan = build_fib_grid(
        mark,
        state.is_long,
        new_swing,
        levels=args.levels,
        base_usdt=args.base_size,
        level_usdt=args.level_size,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        filt=filt,
        tp_ext=args.fib_tp_ext,
        sl_buf_pct=args.fib_sl_buf,
        fvg=state.plan.fvg,
    )
    if not new_plan.levels:
        return False

    print(
        f"{CYAN}↗ Raise top · swing "
        f"{price_fmt(old.low)}→{price_fmt(old.high)}  ⇒  "
        f"{price_fmt(new_swing.low)}→{price_fmt(new_swing.high)} "
        f"· TP {price_fmt(new_plan.tp_price)} · re-arm grid{RESET}"
    )
    append_journal(
        symbol,
        f"RAISE swing {old.low:.8g}->{old.high:.8g} => "
        f"{new_swing.low:.8g}->{new_swing.high:.8g} tp={new_plan.tp_price:.8g}",
    )
    cancel_our_grid(symbol, api, sec, recv)
    n = place_grid_limits(symbol, new_plan, filt, hedge, api, sec, recv)
    append_journal(symbol, f"REARM grid={n}/{len(new_plan.levels)}")
    state.plan = new_plan
    state.tp = new_plan.tp_price
    state.sl = new_plan.sl_price
    state.armed_at = now  # refresh timeout window
    state.swing_high_anchor = new_swing.high if state.is_long else new_swing.low
    state.filled_levels.clear()
    return True


def tick_pending(
    symbol: str,
    state: CycleState,
    mark: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    args: argparse.Namespace,
) -> None:
    """While waiting for pullback: raise top if impulse extends, arm exits on fill, or disarm."""
    if not state.pending or state.plan is None:
        return

    timeout = float(getattr(args, "arm_timeout_sec", 900.0))
    age = time.time() - state.armed_at
    qty, entry = get_position(symbol, state.is_long, hedge, api, sec, recv)

    # Invalidation: price ran through SL before we got filled
    if qty <= 0 and state.sl > 0:
        if state.is_long and mark <= state.sl:
            disarm_pending(symbol, state, api, sec, recv, reason="sl_before_fill")
            return
        if not state.is_long and mark >= state.sl:
            disarm_pending(symbol, state, api, sec, recv, reason="sl_before_fill")
            return

    # Invalidation: mark broke through impulse origin while still pending
    if qty <= 0 and state.plan is not None and state.plan.swing is not None:
        if mark_through_origin(state.plan.swing, mark, is_long=state.is_long):
            disarm_pending(symbol, state, api, sec, recv, reason="through_origin")
            return

    # Timeout with no fill
    if qty <= 0 and age >= timeout:
        disarm_pending(symbol, state, api, sec, recv, reason=f"timeout_{timeout:g}s")
        return

    if qty <= 0:
        try_raise_pending_top(symbol, state, mark, filt, hedge, api, sec, recv, args)
        open_idxs, filled_idxs = sync_grid_level_status(symbol, state, api, sec, recv)
        print_live_table(
            state,
            render_live_table(
                symbol, state, mark,
                open_limits=len(open_idxs),
                age_sec=age,
                open_level_idxs=open_idxs,
                filled_level_idxs=filled_idxs,
            ),
        )
        return

    # First fill — promote to active and arm TP (avg by default)
    if not state.exits_armed:
        clear_live_table(state)
        tp_mode = str(getattr(args, "tp_mode", TP_MODE_DEFAULT))
        tp_target = resolve_tp_price(
            state.is_long, entry, state.plan,
            tp_mode=tp_mode, tp_pct=float(getattr(args, "tp_pct", 0.35)),
            first_fill=True,
        )
        sl_target = state.plan.sl_price
        print(
            f"{GREEN}✓ Pullback fill qty={qty:g} @ {price_fmt(entry)} — "
            f"arming TP {price_fmt(tp_target)} (1st=swing max) / SL {price_fmt(sl_target)}{RESET}"
        )
        try:
            tp, sl = place_exchange_exits(
                symbol, state.is_long, qty, entry, mark or entry,
                tp_target, sl_target, filt, hedge, api, sec, recv,
            )
            state.tp, state.sl = tp, sl
            state.exits_armed = True
            append_journal(
                symbol,
                f"OPEN {'LONG' if state.is_long else 'SHORT'} qty={qty:g} "
                f"entry={entry:.8g} tp={tp:.8g} sl={sl:.8g} tp_mode={tp_mode}",
            )
            tg = _tg()
            if tg is not None:
                tg.notify_fib_open(
                    symbol, "LONG" if state.is_long else "SHORT",
                    qty, entry, tp=tp, sl=sl, vol_usdt=qty * entry,
                )
        except Exception as exc:
            print(f"{RED}Exit arm failed after fill: {exc}{RESET}")
            append_journal(symbol, f"ERROR exits_after_fill {exc}")
            tg = _tg()
            if tg is not None:
                tg.notify_fib_error(symbol, f"exits after fill: {exc}")

    state.active = True
    state.pending = False
    state.entry = entry
    state.last_qty = qty
    state.opened_at = time.time()


def close_cycle_cleanup(symbol: str, state: CycleState, api: str, sec: str, recv: int) -> None:
    clear_live_table(state)
    n_g = cancel_our_grid(symbol, api, sec, recv)
    n_e = cancel_our_exits(symbol, api, sec, recv)
    if n_g or n_e:
        print(f"{DIM}Cleanup cancelled grid={n_g} exits={n_e}{RESET}")
    append_journal(symbol, f"FLAT cleanup grid={n_g} exits={n_e}")
    tg = _tg()
    if tg is not None and (state.last_qty > 0 or state.last_notional > 0):
        tg.notify_fib_closed(
            symbol, "LONG" if state.is_long else "SHORT",
            vol_usdt=state.last_notional or None,
            leverage=state.last_leverage or None,
            pnl_usdt=state.last_upnl,
        )
    state.active = False
    state.pending = False
    state.exits_armed = False
    state.trail_armed = False
    state.plan = None
    state.last_qty = 0.0
    state.last_notional = 0.0
    state.last_upnl = 0.0
    state.filled_levels.clear()


# keep name used elsewhere
open_cycle = arm_cycle


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
    """When DCA fills grow qty, re-arm TP/SL (or trailing) for full position."""
    if not state.active or state.plan is None:
        return
    qty, entry = get_position(symbol, state.is_long, hedge, api, sec, recv)
    if qty <= 0:
        return

    grown = qty > state.last_qty * 1.02
    if grown:
        clear_live_table(state)
        prev_qty = state.last_qty
        print(f"{CYAN}Position grew {state.last_qty:g} → {qty:g} — refreshing exits{RESET}")
        meta = get_position_meta(symbol, state.is_long, hedge, api, sec, recv)
        upnl = float(meta.get("unrealized_pnl") or 0)
        notional = float(meta.get("notional") or (qty * entry))
        lev = int(meta.get("leverage") or 0)
        fill_qty = max(0.0, qty - prev_qty)
        old_notional = float(state.last_notional or 0)
        fill_notional = max(0.0, notional - old_notional)
        fill_price = (fill_notional / fill_qty) if fill_qty > 0 else entry
        if state.trail_armed:
            callback = float(
                getattr(args, "protect_trail_callback", state.trail_callback) or 0.2
            )
            try:
                resp = place_protect_trailing(
                    symbol, state.is_long, qty, filt, hedge, api, sec, recv, callback,
                )
                state.trail_callback = callback
                state.entry = entry
                state.sl = entry
                print(
                    f"{GREEN}✓ Trail refreshed qty={qty:g} cb={callback:g}% "
                    f"(algoId={resp.get('algoId')}){RESET}"
                )
                append_journal(
                    symbol,
                    f"PROTECT_TRAIL refresh cb={callback:g} qty={qty:g}",
                )
            except Exception as exc:
                print(f"{RED}Trail refresh failed: {exc}{RESET}")
        else:
            tp_mode = str(getattr(args, "tp_mode", TP_MODE_DEFAULT))
            tp = resolve_tp_price(
                state.is_long, entry, state.plan,
                tp_mode=tp_mode, tp_pct=float(getattr(args, "tp_pct", 0.35)),
                first_fill=False,
            )
            sl = state.plan.sl_price if state.plan else state.sl
            print(
                f"{DIM}TP → {price_fmt(tp)} (compensation · {tp_mode} from avg {price_fmt(entry)}){RESET}"
            )
            try:
                tp, sl = place_exchange_exits(
                    symbol, state.is_long, qty, entry, mark or entry,
                    tp, sl, filt, hedge, api, sec, recv,
                )
                state.tp, state.sl = tp, sl
                state.entry = entry
                append_journal(
                    symbol,
                    f"EXITS refresh tp={tp:.8g} sl={sl:.8g} qty={qty:g} tp_mode={tp_mode}",
                )
            except Exception as exc:
                print(f"{RED}Exit refresh failed: {exc}{RESET}")
        tg = _tg()
        if tg is not None and fill_qty > 0:
            tg.notify_fib_fill(
                symbol, "LONG" if state.is_long else "SHORT",
                fill_qty, fill_price, qty, entry,
                vol_usdt=notional, leverage=lev, pnl_usdt=upnl,
            )
        state.last_qty = qty
        state.last_notional = notional
        state.last_upnl = upnl
        state.last_leverage = lev

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


# ── main loop ────────────────────────────────────────────────────────────────

def mid_from_depth(depth: dict) -> float:
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    if not bids or not asks:
        return 0.0
    return (float(bids[0][0]) + float(asks[0][0])) / 2.0


def run(args: argparse.Namespace) -> int:
    load_env_file(args.env_file)
    if getattr(args, "dry_run", False):
        args.execute = False
    apply_entry_sizing(args)
    sym = args.symbol.upper()
    api, sec = load_keys(args.env_file)
    if args.execute and (not api or not sec):
        print(f"{RED}No API keys — cannot execute{RESET}")
        return 1

    filt = load_symbol_filters(sym)
    hedge = False
    lev = 0
    if args.execute:
        hedge = _resolve_hedge(args, api, sec)
        lev = ensure_symbol_leverage(sym, api, sec, args.recv_window, args)

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
    margin_hint = ""
    if lev > 0 and args.base_size > 0:
        margin_hint = f" · lev {lev}x · margin~{args.base_size / lev:.2f}U"
    print(
        f"{BOLD}{CYAN}Micro-grid{RESET} {sym}  {mode}\n"
        f"{DIM}grid={args.grid_mode} · fib {args.fib_interval} minΔ{args.fib_min_range:g}% · "
        f"arm Fib0–{args.arm_max_fib:g} · "
        f"FVG≥{args.fvg_min_pct:g}%{' req' if args.require_fvg else ''} · "
        f"wait_pullback={'ON' if args.wait_pullback else 'OFF'} "
        f"raise_top={'ON' if args.raise_top else 'OFF'} "
        f"arm≤{args.arm_timeout_sec:g}s · "
        f"bar {args.bar_sec:g}s · levels {args.levels} · "
        f"base {args.base_size:g} · level {args.level_size:g} USDT"
        f"{margin_hint} · "
        f"TP={args.tp_mode}+{args.tp_pct:g}% · SL {args.sl_pct:g}% · "
        f"protect_trail={'ON' if args.protect_trail else 'OFF'}"
        f"(cb={args.protect_trail_callback:g}%) · "
        f"cooldown={args.cooldown_sec:g}s · "
        f"sweep={'ON' if args.sweep else 'OFF'} · "
        f"dir={args.direction}{RESET}\n"
        f"{DIM}log {journal_path(sym)}{RESET}"
    )
    append_journal(
        sym,
        f"START mode={args.grid_mode} bar={args.bar_sec} levels={args.levels} "
        f"execute={int(args.execute)} lev={lev} base={args.base_size:g}",
    )
    if args.execute:
        pid_path = register_fib_pidfile(sym)
        print(f"{DIM}pidfile {pid_path} (Telegram /stop {sym}){RESET}")
    tg = _tg()
    if tg is not None and args.execute:
        note = (
            f"grid={args.grid_mode} · levels={args.levels} · "
            f"arm≤{args.arm_max_fib:g} · wait_pullback={'ON' if args.wait_pullback else 'OFF'}"
        )
        tg.notify_fib_started(sym, direction=args.direction, note=note)
    elif args.execute:
        print(f"{DIM}Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID){RESET}")

    # If a same-side position is already open, adopt it and show the live table
    if args.execute:
        depth0 = fetch_depth(sym, args.depth_limit)
        mark0 = mid_from_depth(depth0)
        adopt_sides: list[bool] = []
        if args.direction == "long":
            adopt_sides = [True]
        elif args.direction == "short":
            adopt_sides = [False]
        else:
            adopt_sides = [True, False]
        for is_long0 in adopt_sides:
            q0, _ = get_position(sym, is_long0, hedge, api, sec, args.recv_window)
            if q0 > 0 and mark0 > 0:
                adopted = adopt_existing_position(
                    sym, is_long0, mark0, filt, hedge,
                    api, sec, args.recv_window, args,
                )
                if adopted is not None:
                    state = adopted
                    break

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

            # Pending pullback grid (armed, no position yet)
            if args.execute and state.pending and not state.active:
                tick_pending(
                    sym, state, mark, filt, hedge, api, sec, args.recv_window, args,
                )
                if state.pending:
                    time.sleep(max(0.2, args.sample_sec))
                    continue

            # Live position sync
            if args.execute and state.active:
                qty, entry = get_position(sym, state.is_long, hedge, api, sec, args.recv_window)
                if qty <= 0:
                    clear_live_table(state)
                    print(f"{GREEN}Position flat — cycle done{RESET}")
                    close_cycle_cleanup(sym, state, api, sec, args.recv_window)
                    cooldown_until = time.time() + args.cooldown_sec
                else:
                    refresh_exits_if_grown(
                        sym, state, filt, hedge, api, sec, args.recv_window, mark, args,
                    )
                    meta = get_position_meta(sym, state.is_long, hedge, api, sec, args.recv_window)
                    upnl = float(meta.get("unrealized_pnl") or 0)
                    state.last_upnl = upnl
                    state.last_notional = float(meta.get("notional") or (qty * entry))
                    state.last_leverage = int(meta.get("leverage") or 0)
                    open_idxs, filled_idxs = sync_grid_level_status(
                        sym, state, api, sec, args.recv_window,
                    )
                    maybe_arm_full_fill_trail(
                        sym, state, mark, qty, entry, upnl,
                        open_idxs, filled_idxs, filt, hedge,
                        api, sec, args.recv_window, args,
                    )
                    print_live_table(
                        state,
                        render_live_table(
                            sym, state, mark,
                            qty=qty, entry=entry, upnl=upnl,
                            open_level_idxs=open_idxs,
                            filled_level_idxs=filled_idxs,
                        ),
                    )

            # New cycle on bar close when flat and not pending
            if (
                bar is not None
                and not state.active
                and not state.pending
                and time.time() >= cooldown_until
            ):
                direction: str | None
                if args.direction in ("long", "short"):
                    direction = args.direction
                else:
                    direction = entry_signal(bar, sig_cfg)

                if direction:
                    is_long = direction == "long"
                    # Refuse only if THIS side is already open.
                    # In hedge mode the opposite side may stay open (inverse OK).
                    if args.execute:
                        q_same, _ = get_position(
                            sym, is_long, hedge, api, sec, args.recv_window,
                        )
                        if q_same > 0:
                            if not state.active:
                                adopted = adopt_existing_position(
                                    sym, is_long, mark, filt, hedge,
                                    api, sec, args.recv_window, args,
                                )
                                if adopted is not None:
                                    state = adopted
                            builder.reset_after_bar(time.time())
                            time.sleep(args.sample_sec)
                            continue
                        if hedge:
                            q_opp, _ = get_position(
                                sym, not is_long, hedge, api, sec, args.recv_window,
                            )
                            if q_opp > 0:
                                print(
                                    f"{DIM}Hedge: opposite "
                                    f"{'LONG' if not is_long else 'SHORT'} "
                                    f"qty={q_opp:g} stays open{RESET}"
                                )
                        else:
                            # One-way: any exposure on the other side still blocks
                            q_opp, _ = get_position(
                                sym, not is_long, hedge, api, sec, args.recv_window,
                            )
                            if q_opp > 0:
                                print(
                                    f"{YELLOW}Skip — existing position on {sym} "
                                    f"(one-way){RESET}"
                                )
                                builder.reset_after_bar(time.time())
                                time.sleep(args.sample_sec)
                                continue

                    print(
                        f"\n{CYAN}Signal {direction.upper()} "
                        f"imb={bar.imbalance:.3f} Δ={bar.mid_change_pct():+.3f}%{RESET}"
                    )
                    plan = build_grid_plan(sym, mark, is_long, args, filt)
                    if plan is None:
                        builder.reset_after_bar(time.time())
                        time.sleep(args.sample_sec)
                        continue
                    wait = bool(args.wait_pullback) and plan.mode == "fib"
                    try:
                        if args.execute:
                            ensure_symbol_leverage(
                                sym, api, sec, args.recv_window, args,
                            )
                        state = arm_cycle(
                            sym, plan, mark, filt, hedge, api, sec, args.recv_window,
                            dry_run=not args.execute,
                            wait_pullback=wait,
                            args=args,
                        )
                        if not args.execute:
                            cooldown_until = time.time() + args.cooldown_sec
                    except Exception as exc:
                        print(f"{RED}Arm cycle failed: {exc}{RESET}")
                        append_journal(sym, f"ERROR arm {exc}")
                        tg = _tg()
                        if tg is not None:
                            tg.notify_fib_error(sym, f"arm: {exc}")
                        if args.execute:
                            cancel_our_grid(sym, api, sec, args.recv_window)
                            cancel_our_exits(sym, api, sec, args.recv_window)
                        cooldown_until = time.time() + args.cooldown_sec

                    builder.reset_after_bar(time.time())
                    if args.once and (state.active or state.pending or not args.execute):
                        return 0
                    continue

                builder.reset_after_bar(time.time())
            elif bar is not None:
                builder.reset_after_bar(time.time())

            time.sleep(max(0.2, args.sample_sec))
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}")
        if args.execute and state.active:
            print(f"{YELLOW}Leaving position + grid/exits on book (Ctrl+C does not flatten){RESET}")
        return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="15s micro-grid: full grid + TP/SL at open")
    p.add_argument("symbol", help="Futures symbol, e.g. UBUSDT")
    p.add_argument("--execute", action=argparse.BooleanOptionalAction, default=True,
                   help="Send live orders (default on; use --dry-run or --no-execute to plan only)")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan only — no orders (overrides default --execute)")
    p.add_argument("--env-file", default="", help="Path to .env")
    p.add_argument("--recv-window", type=int, default=15000)
    p.add_argument("--position-mode", choices=("auto", "hedge", "oneway"), default="auto")
    p.add_argument("--direction", choices=("auto", "long", "short"), default="auto",
                   help="Trade side (default: auto from 15s order-book signal)")
    p.add_argument(
        "--grid-mode",
        choices=("fib", "step"),
        default=(os.getenv("OB_MG_GRID_MODE", "fib").strip().lower() or "fib"),
        help="Grid construction (default: fib)",
    )
    p.add_argument("--fib-interval", default=os.getenv("OB_MG_FIB_INTERVAL", "1m").strip() or "1m",
                   help="Kline TF for Fib swing (default 1m; use 5m for slower swings)")
    p.add_argument("--fib-lookback", type=int, default=_env_int("OB_MG_FIB_LOOKBACK", 40))
    p.add_argument("--fib-min-range", type=float, default=_env_float("OB_MG_FIB_MIN_RANGE", 0.40),
                   help="Min swing range %% on fib interval")
    p.add_argument("--fib-max-span", type=int, default=_env_int("OB_MG_FIB_MAX_SPAN", 12))
    p.add_argument("--fib-tp-ext", type=float, default=_env_float("OB_MG_FIB_TP_EXT", FIB_TP_EXT))
    p.add_argument("--fib-sl-buf", type=float, default=_env_float("OB_MG_FIB_SL_BUF", FIB_SL_BUF),
                   help="%% buffer beyond swing origin for SL")
    p.add_argument("--arm-max-fib", type=float, default=_env_float("OB_MG_ARM_MAX_FIB", FIB_ARM_MAX),
                   help="Arm only while pullback depth ≤ this Fib (0=extreme … 0.236 default)")
    # Back-compat alias
    p.add_argument("--arm-min-fib", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--fvg-min-pct", type=float, default=_env_float("OB_MG_FVG_MIN_PCT", FVG_MIN_PCT),
                   help="Min FVG height %% of mid (default 0.08; suited to 1m)")
    p.add_argument("--require-fvg", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_MG_REQUIRE_FVG", False),
                   help="Fib mode: hard-skip without aligned FVG (default off; FVG still preferred)")
    p.add_argument("--wait-pullback", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_MG_WAIT_PULLBACK", True),
                   help="Fib: LIMIT grid only, no MARKET; TP after fill @ swing max (default on)")
    p.add_argument("--raise-top", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_MG_RAISE_TOP", True),
                   help="While pending, raise Fib/TP if impulse makes new high/low (default on)")
    p.add_argument("--raise-min-pct", type=float, default=_env_float("OB_MG_RAISE_MIN_PCT", 0.05),
                   help="Min %% extension to trigger a raise (default 0.05)")
    p.add_argument("--arm-timeout-sec", type=float,
                   default=_env_float("OB_MG_ARM_TIMEOUT_SEC", 900.0),
                   help="Disarm unfilled pullback grid after N seconds (default 900)")
    p.add_argument("--levels", type=int, default=_env_int("OB_MG_LEVELS", 4))
    p.add_argument("--step-pct", type=float, default=_env_float("OB_MG_STEP_PCT", 0.08),
                   help="Only for --grid-mode step")
    p.add_argument("--entry-usdt", type=float, default=_env_float("OB_MG_ENTRY_USDT", 0.0),
                   help="Entry notional USDT (sets --base-size; uses max leverage by default)")
    p.add_argument("--base-size", type=float, default=_env_float("OB_MG_BASE_SIZE", 10.0),
                   help="First rung notional USDT (overridden by --entry-usdt)")
    p.add_argument("--level-size", type=float, default=_env_float("OB_MG_LEVEL_SIZE", 8.0),
                   help="Deeper rung notional USDT")
    p.add_argument("--set-leverage", type=int, default=_env_int("OB_MG_SET_LEVERAGE", 0),
                   help="Force leverage (0 = use symbol max)")
    p.add_argument("--no-max-leverage", action="store_true",
                   default=_env_bool("OB_MG_NO_MAX_LEVERAGE", False),
                   help="Do not raise leverage to symbol max before arming")
    p.add_argument("--tp-mode", choices=("avg", "swing"),
                   default=(os.getenv("OB_MG_TP_MODE", TP_MODE_DEFAULT).strip().lower() or TP_MODE_DEFAULT),
                   help="TP: avg=1st fill @ swing max then avg+tp%% (default); swing=always impulse extreme")
    p.add_argument("--tp-pct", type=float, default=_env_float("OB_MG_TP_PCT", 0.35),
                   help="With --tp-mode avg: %% above/below live average (default 0.35)")
    p.add_argument("--sl-pct", type=float, default=_env_float("OB_MG_SL_PCT", 0.50))
    p.add_argument(
        "--protect-trail",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("OB_MG_PROTECT_TRAIL", True),
        help="After all --levels fill, replace SL with trailing once in profit (default on)",
    )
    p.add_argument(
        "--protect-trail-callback",
        type=float,
        default=_env_float("OB_MG_PROTECT_TRAIL_CALLBACK", 0.2),
        help="Trailing callbackRate %% (default 0.2; also min profit before arm)",
    )
    p.add_argument(
        "--protect-arm-pnl-pct",
        type=float,
        default=_env_float("OB_MG_PROTECT_ARM_PNL_PCT", 0.0),
        help="Extra min mark profit %% before arming trail (default 0; effective min=callback)",
    )
    p.add_argument("--bar-sec", type=float, default=_env_float("OB_MG_BAR_SEC", 15.0))
    p.add_argument("--sample-sec", type=float, default=_env_float("OB_MG_SAMPLE_SEC", 1.0))
    p.add_argument("--band-pct", type=float, default=1.0)
    p.add_argument("--depth-limit", type=int, default=50)
    p.add_argument("--imb-long", type=float, default=0.55)
    p.add_argument("--imb-short", type=float, default=0.45)
    p.add_argument("--momentum-min-pct", type=float, default=0.01)
    p.add_argument("--cooldown-sec", type=float,
                   default=_env_float("OB_MG_COOLDOWN_SEC", 3600.0),
                   help="Seconds to wait after flat/cycle before re-arm (default 3600 = 1h)")
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
        qty, entry = get_position(sym, is_long, hedge, api, sec, args.recv_window)
        if qty > 0:
            meta = get_position_meta(sym, is_long, hedge, api, sec, args.recv_window)
            filt = load_symbol_filters(sym)
            market_close_position(sym, is_long, qty, hedge, filt, api, sec, args.recv_window)
            side = "LONG" if is_long else "SHORT"
            print(f"{GREEN}Flattened {side} {qty:g}{RESET}")
            append_journal(sym, f"FLATTEN {side} qty={qty:g}")
            tg = _tg()
            if tg is not None:
                tg.notify_fib_closed(
                    sym, side,
                    vol_usdt=float(meta.get("notional") or (qty * entry)),
                    leverage=int(meta.get("leverage") or 0) or None,
                    pnl_usdt=float(meta.get("unrealized_pnl") or 0),
                )
    return 0


def main() -> int:
    load_env_file("")
    args = build_arg_parser().parse_args()
    if args.no_sweep:
        args.sweep = False
    if args.dry_run:
        args.execute = False
    if args.flatten:
        return flatten_and_exit(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
