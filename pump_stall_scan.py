#!/usr/bin/env python3
"""Detect pump → stall → short-grid candidates on Binance USDT-M perps.

1D structure filter (same judgment as reading the daily chart):
  1. Large recent trough→peak pump
  2. Still near that peak AND near the ~30–45d regime high (rejects downtrend bounces)
  3. Move is sharp / recent (blow-off), not a slow grind
  4. Starting to stall on daily ranges
  5. Enough ask-side order-book walls for a SHORT DCA grid

Display-only by default. With --watch --auto-trade: run the top
`--max-trades` ★ from this list (default 3) via `dca SYMBOL short --once`.
Primary exit via `--trade-exit` / .env TRADE_EXIT|EXIT_MODE
  (structure|ob|trailing|pullback|ratchet…); BE is optional
via `--protect-be` / `--no-protect-be` (orthogonal).
Early profile: `--trade-exit ob --protect-be`.
Other open pairs on the account do not consume these slots.
Account Margin Ratio (Binance UI): ≥ soft (default 5%) → no new ★;
≥ hard (default 8%) → cancel DCA limits (keep exits); below hard → re-arm DCA.

  python3 pump_stall_scan.py
  ./pump-stall --top 15 --min-near-regime 80 --min-sharp 35
  ./pump-stall --watch --auto-trade --max-trades 3 --interval 60

Profiles (wrappers; defaults of ./pump-stall-watch stay strict):
  ./pump-stall-watch          # stall≥35 · near≥85 · ★≥92
  ./pump-stall-watch-early    # TEST: stall≥25 · near≥82 · ★≥90
                              # + auto-trade · BE + trail@1.5%/0.6%
  ./pump-stall-early          # one-shot scan with the early profile
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from futures_scan import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    FAPI_BASE,
    TickerRow,
    fetch_klines,
    fetch_tickers,
    trading_usdt_perps,
)
from orderbook_dca_grid import fetch_depth, select_walls

# Extra ANSI for filter tags (not in futures_scan)
MAGENTA = "\033[35m"
BLUE = "\033[34m"
ORANGE = "\033[38;5;208m"
WHITE = "\033[37m"

ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / ".state" / "pump_stall_snapshot.json"

# First failing filter → color + short label
BLOCK_COLORS = {
    "klines": DIM,
    "pump": MAGENTA,
    "near": YELLOW,
    "regime": CYAN,
    "sharp": BLUE,
    "peak": WHITE,
    "stall": ORANGE,
    "walls": RED,
}


@dataclass
class PumpStallHit:
    symbol: str
    last: float
    change_24h: float
    quote_volume: float
    pump_7d_pct: float
    near_high_pct: float  # last / recent-peak * 100
    near_regime_pct: float  # last / 30d-high * 100 (filters downtrend bounces)
    sharp_pct: float  # % of pump that happened in last N days
    stall_score: float  # 0..100 higher = more stalled
    ask_walls: int
    ask_span_pct: float
    wall_prices: list[float]
    score: float
    note: str


@dataclass
class AnalyzeRow:
    """Pass (hit set) or first filter that blocked the seed symbol."""
    symbol: str
    change_24h: float
    quote_volume: float
    hit: PumpStallHit | None = None
    blocked_by: str | None = None  # pump|near|regime|sharp|peak|stall|walls|klines
    pump_7d_pct: float = 0.0
    near_high_pct: float = 0.0
    near_regime_pct: float = 0.0
    sharp_pct: float = 0.0
    stall_score: float = 0.0
    peak_is_recent: bool = False
    ask_walls: int = 0
    detail: str = ""  # human value that failed, e.g. "near 81%<85"


def _ohlc(klines: list[list]) -> list[tuple[float, float, float, float]]:
    """Return [(o,h,l,c), ...] oldest→newest."""
    out: list[tuple[float, float, float, float]] = []
    for k in klines:
        try:
            out.append((float(k[1]), float(k[2]), float(k[3]), float(k[4])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


@dataclass
class _DayMetrics:
    pump_pct: float
    near_high_pct: float
    near_regime_pct: float
    sharp_pct: float
    stall_score: float
    peak_is_recent: bool


def _day_metrics(daily: list[tuple[float, float, float, float]], *, sharp_days: int = 5) -> _DayMetrics | None:
    """1D structure: blow-off near regime high vs bounce in a downtrend."""
    if len(daily) < 10:
        return None
    highs = [h for _, h, _, _ in daily]
    lows = [lo for _, _, lo, _ in daily]
    closes = [c for _, _, _, c in daily]
    ranges = [(h - lo) / c * 100 if c > 0 else 0.0 for _, h, lo, c in daily]

    # Recent window (~2w) for the active pump
    recent = daily[-14:] if len(daily) >= 14 else daily
    r_highs = [h for _, h, _, _ in recent]
    r_lows = [lo for _, _, lo, _ in recent]
    r_closes = [c for _, _, _, c in recent]

    peak = max(r_highs)
    trough = min(r_lows)
    last = r_closes[-1]
    pump = ((peak - trough) / trough * 100) if trough > 0 else 0.0
    near = (last / peak * 100) if peak > 0 else 0.0

    # Regime high over full 1D lookback (≈30–45d) — bounce in downtrend fails this
    regime_high = max(highs)
    near_regime = (last / regime_high * 100) if regime_high > 0 else 0.0
    peak_idx = highs.index(max(highs))
    # Peak of whole window should be in the last sharp_days+2 bars (top is NOW)
    peak_is_recent = peak_idx >= len(daily) - (sharp_days + 2)

    # Sharpness: how much of trough→now happened in the last sharp_days closes
    n = min(sharp_days, len(r_closes) - 1)
    base_px = r_closes[-(n + 1)]
    recent_leg = ((last - base_px) / trough * 100) if trough > 0 else 0.0
    sharp = max(0.0, min(100.0, (recent_leg / pump * 100) if pump > 1e-9 else 0.0))

    # Also reward a fat daily candle in the last week (parabolic print)
    max_up = 0.0
    for o, h, lo, c in recent[-7:]:
        body = (c - o) / o * 100 if o > 0 else 0.0
        max_up = max(max_up, body)
    if max_up >= 12:
        sharp = min(100.0, sharp + 15)
    if max_up >= 25:
        sharp = min(100.0, sharp + 10)

    # Stall on 1D: last 3 ranges shrink vs earlier recent bars
    early = ranges[-14:-3] or ranges[:-3] or ranges
    late = ranges[-3:]
    avg_early = sum(early) / len(early) if early else 0.0
    avg_late = sum(late) / len(late) if late else 0.0
    shrink = 0.0
    if avg_early > 1e-9:
        shrink = max(0.0, min(1.0, 1.0 - (avg_late / avg_early)))

    last3 = closes[-3:]
    mean = sum(last3) / len(last3)
    var = sum((x - mean) ** 2 for x in last3) / len(last3)
    std_pct = ((var ** 0.5) / last * 100) if last > 0 else 99.0
    flat = max(0.0, min(1.0, 1.0 - std_pct / 8.0))

    hh_prev = max(highs[:-1]) if len(highs) > 1 else highs[0]
    hh_last = highs[-1]
    no_new_high = 1.0 if hh_last <= hh_prev * 1.002 else 0.35

    stall = 100.0 * (0.45 * shrink + 0.35 * flat + 0.20 * no_new_high)
    return _DayMetrics(
        pump_pct=pump,
        near_high_pct=near,
        near_regime_pct=near_regime,
        sharp_pct=sharp,
        stall_score=stall,
        peak_is_recent=peak_is_recent,
    )


def _ask_grid(symbol: str, *, so_count: int, min_gap: float, min_dist: float,
              max_range: float, limit: int) -> tuple[int, float, list[float]]:
    try:
        depth = fetch_depth(symbol, limit)
    except Exception:
        return 0, 0.0, []
    bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]
    if not bids or not asks:
        return 0, 0.0, []
    mid = (bids[0][0] + asks[0][0]) / 2
    span = ((asks[-1][0] - mid) / mid * 100) if mid > 0 else 0.0
    walls = select_walls(asks, mid, False, so_count, min_gap, min_dist, max_range)
    prices = [float(p) for p, _, _ in walls]
    return len(walls), span, prices


def _analyze_one(
    t: TickerRow,
    *,
    base: str,
    min_pump_7d: float,
    min_near_high: float,
    min_near_regime: float,
    min_sharp: float,
    require_recent_peak: bool,
    min_stall: float,
    min_walls: int,
    so_count: int,
    min_gap: float,
    min_dist: float,
    max_range: float,
    depth_limit: int,
) -> AnalyzeRow:
    base_row = AnalyzeRow(
        symbol=t.symbol,
        change_24h=t.change_pct,
        quote_volume=t.quote_volume,
    )
    try:
        # ~45 daily bars: enough to see downtrend vs blow-off at the right edge
        kl = fetch_klines(base, t.symbol, "1d", 45)
    except Exception:
        base_row.blocked_by = "klines"
        base_row.detail = "klines fail"
        return base_row
    daily = _ohlc(kl)
    m = _day_metrics(daily)
    if m is None:
        base_row.blocked_by = "klines"
        base_row.detail = "not enough bars"
        return base_row

    base_row.pump_7d_pct = m.pump_pct
    base_row.near_high_pct = m.near_high_pct
    base_row.near_regime_pct = m.near_regime_pct
    base_row.sharp_pct = m.sharp_pct
    base_row.stall_score = m.stall_score
    base_row.peak_is_recent = m.peak_is_recent

    if m.pump_pct < min_pump_7d:
        base_row.blocked_by = "pump"
        base_row.detail = f"pump {m.pump_pct:.0f}%<{min_pump_7d:g}"
        return base_row
    if m.near_high_pct < min_near_high:
        base_row.blocked_by = "near"
        base_row.detail = f"near {m.near_high_pct:.0f}%<{min_near_high:g}"
        return base_row
    # Reject "bounce in a larger downtrend" (ZBT): still far below 1D regime high
    if m.near_regime_pct < min_near_regime:
        base_row.blocked_by = "regime"
        base_row.detail = f"reg {m.near_regime_pct:.0f}%<{min_near_regime:g}"
        return base_row
    if m.sharp_pct < min_sharp:
        base_row.blocked_by = "sharp"
        base_row.detail = f"sharp {m.sharp_pct:.0f}<{min_sharp:g}"
        return base_row
    if require_recent_peak and not m.peak_is_recent:
        base_row.blocked_by = "peak"
        base_row.detail = "peak not recent"
        return base_row
    if m.stall_score < min_stall:
        base_row.blocked_by = "stall"
        base_row.detail = f"stall {m.stall_score:.0f}<{min_stall:g}"
        return base_row

    walls_n, span, prices = _ask_grid(
        t.symbol,
        so_count=so_count,
        min_gap=min_gap,
        min_dist=min_dist,
        max_range=max_range,
        limit=depth_limit,
    )
    base_row.ask_walls = walls_n
    if walls_n < min_walls:
        base_row.blocked_by = "walls"
        base_row.detail = f"walls {walls_n}<{min_walls}"
        return base_row

    score = (
        min(m.pump_pct, 200) * 0.25
        + m.stall_score * 0.20
        + m.near_high_pct * 0.10
        + m.near_regime_pct * 0.15
        + m.sharp_pct * 0.15
        + min(walls_n, so_count) / so_count * 100 * 0.10
        + min(span, 12) / 12 * 100 * 0.05
    )
    if t.change_pct >= 15:
        score += 8
    note_bits = [
        f"1D regime {m.near_regime_pct:.0f}%",
        f"sharp {m.sharp_pct:.0f}",
        f"stall {m.stall_score:.0f}",
        f"{walls_n} walls / {span:.1f}%",
    ]
    if t.change_pct >= 20:
        note_bits.insert(0, f"24h +{t.change_pct:.0f}%")
    hit = PumpStallHit(
        symbol=t.symbol,
        last=t.last,
        change_24h=t.change_pct,
        quote_volume=t.quote_volume,
        pump_7d_pct=m.pump_pct,
        near_high_pct=m.near_high_pct,
        near_regime_pct=m.near_regime_pct,
        sharp_pct=m.sharp_pct,
        stall_score=m.stall_score,
        ask_walls=walls_n,
        ask_span_pct=span,
        wall_prices=prices,
        score=score,
        note=" · ".join(note_bits),
    )
    base_row.hit = hit
    return base_row


def scan(args: argparse.Namespace) -> tuple[list[PumpStallHit], list[AnalyzeRow]]:
    base = args.base.rstrip("/")
    allowed = trading_usdt_perps(base)
    tickers = fetch_tickers(base, allowed)
    liquid = [t for t in tickers if t.quote_volume >= args.min_quote_vol]
    # Seed pool: top gainers + high absolute movers (captures multi-day pumps still hot)
    by_chg = sorted(liquid, key=lambda t: t.change_pct, reverse=True)
    pool: dict[str, TickerRow] = {}
    for t in by_chg[: args.pool_gainers]:
        pool[t.symbol] = t
    # Also include anything already up a lot on 24h even outside top-N
    for t in liquid:
        if t.change_pct >= args.min_change_24h:
            pool[t.symbol] = t
    # Volume leaders among gainers (liquidity for a grid)
    for t in sorted(liquid, key=lambda x: x.quote_volume, reverse=True)[: args.pool_volume]:
        if t.change_pct >= args.min_change_24h * 0.5:
            pool[t.symbol] = t

    candidates = list(pool.values())
    print(
        f"{DIM}Universe {len(liquid)} liquid · seed {len(candidates)} "
        f"(gainers/vol) · 1D blow-off + stall + ask walls…{RESET}",
        flush=True,
    )

    rows: list[AnalyzeRow] = []
    workers = max(1, min(args.workers, 12))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _analyze_one,
                t,
                base=base,
                min_pump_7d=args.min_pump_7d,
                min_near_high=args.min_near_high,
                min_near_regime=args.min_near_regime,
                min_sharp=args.min_sharp,
                require_recent_peak=args.require_recent_peak,
                min_stall=args.min_stall,
                min_walls=args.min_walls,
                so_count=args.so_count,
                min_gap=args.min_gap,
                min_dist=args.min_dist,
                max_range=args.max_range,
                depth_limit=args.limit,
            ): t.symbol
            for t in candidates
        }
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            if done % 20 == 0 or done == len(futs):
                print(f"{DIM}  … {done}/{len(futs)}{RESET}", flush=True)
            try:
                row = fut.result()
            except Exception:
                continue
            if row:
                rows.append(row)

    hits = [r.hit for r in rows if r.hit is not None]
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[: args.top]

    blocked = [r for r in rows if r.hit is None and r.blocked_by]
    # Prefer near-misses (failed late filters) then by 24h change
    _late = {"stall", "walls", "peak", "sharp", "near", "regime"}
    blocked.sort(
        key=lambda r: (
            0 if r.blocked_by in _late else 1,
            -r.change_24h,
            -r.near_high_pct,
        ),
    )
    return hits, blocked


def _block_tag(reason: str) -> str:
    c = BLOCK_COLORS.get(reason, DIM)
    return f"{c}{BOLD}{reason:<6}{RESET}"


def _hit_to_dict(h: PumpStallHit) -> dict:
    return {
        "symbol": h.symbol,
        "change_24h": h.change_24h,
        "pump_7d_pct": h.pump_7d_pct,
        "near_high_pct": h.near_high_pct,
        "near_regime_pct": h.near_regime_pct,
        "sharp_pct": h.sharp_pct,
        "stall_score": h.stall_score,
        "ask_walls": h.ask_walls,
        "ask_span_pct": h.ask_span_pct,
        "score": h.score,
        "note": h.note,
        "wall_prices": list(h.wall_prices[:6]),
        "flag": "",
    }


def _blocked_to_dict(r: AnalyzeRow) -> dict:
    return {
        "symbol": r.symbol,
        "change_24h": r.change_24h,
        "pump_7d_pct": r.pump_7d_pct,
        "near_high_pct": r.near_high_pct,
        "near_regime_pct": r.near_regime_pct,
        "sharp_pct": r.sharp_pct,
        "stall_score": r.stall_score,
        "ask_walls": r.ask_walls,
        "blocked_by": r.blocked_by or "?",
        "detail": r.detail,
    }


def stack_params(args: argparse.Namespace | None = None) -> dict:
    """Live exit/stack knobs for Pumpstall web (help + scanner)."""
    def g(name: str, default):
        if args is None:
            return default
        v = getattr(args, name, default)
        return default if v is None else v

    be_arm = 1.0
    be_profit = 0.3
    tp_partial = 70.0
    tp1_profit = 0.3
    partial_entry_pct = 500.0
    post_arm = float(g("post_be_arm_pct", 1.5) or 1.5)
    post_cb = float(g("post_be_callback", 0.6) or 0.6)
    try:
        wallet_pct = float(os.getenv("WALLET_PCT", "10") or 10)
    except (TypeError, ValueError):
        wallet_pct = 10.0
    try:
        imb_long = float(g("imb_long", 0.55) or 0.55)
    except (TypeError, ValueError):
        imb_long = 0.55
    trade_exit = "structure"
    protect_be = True
    if args is not None:
        trade_exit = _trade_exit_mode(args)
        protect_be = _protect_be_for_trade(args)
    # Size-boost auto knobs (see size_boost.py) — for Help page
    try:
        import size_boost as sb

        boost_mult = float(sb.default_mult())
        boost_dwell = int(sb.auto_dwell_cycles())
        boost_ttl_h = float(sb.auto_ttl_hours())
        boost_auto = bool(sb.auto_enabled())
        boost_strict_stall = float(sb.STRICT_MIN_STALL)
        boost_strict_near = float(sb.STRICT_MIN_NEAR)
        boost_strict_ideal = float(sb.STRICT_IDEAL_NEAR)
    except Exception:  # noqa: BLE001
        boost_mult = 1.5
        boost_dwell = 2
        boost_ttl_h = 5.0
        boost_auto = True
        boost_strict_stall = 35.0
        boost_strict_near = 85.0
        boost_strict_ideal = 92.0

    return {
        "trade_exit": trade_exit,
        "protect_be": protect_be,
        "be_arm_pct": be_arm,
        "be_profit_pct": be_profit,
        "post_be_arm_pct": post_arm,
        "post_be_callback": post_cb,
        "tp_partial_pct": tp_partial,
        "tp1_profit_pct": tp1_profit,
        "partial_tp_min_entry_pct": partial_entry_pct,
        "imb_long": imb_long,
        "loss_cooldown_min": float(g("loss_cooldown_min", 1440.0) or 1440.0),
        "margin_ratio_soft": float(g("margin_ratio_soft", 5.0) or 5.0),
        "margin_ratio_hard": float(g("margin_ratio_hard", 8.0) or 8.0),
        "wallet_pct": wallet_pct,
        "min_gap": float(g("min_gap", 0.8) or 0.8),
        "so_count": int(g("so_count", 8) or 8),
        "max_trades": _max_trades(
            args if args is not None else argparse.Namespace(max_trades=3),
        ),
        "boost_mult": boost_mult,
        "boost_dwell": boost_dwell,
        "boost_ttl_h": boost_ttl_h,
        "boost_auto": 1 if boost_auto else 0,
        "boost_strict_stall": boost_strict_stall,
        "boost_strict_near": boost_strict_near,
        "boost_strict_ideal": boost_strict_ideal,
        # Risk-reduce addon (SHORT) — Help page
        "risk_reduce": (
            0
            if (os.getenv("RISK_REDUCE", "1") or "1").strip().lower()
            in ("0", "false", "off", "no")
            else 1
        ),
        "risk_reduce_pct": float(os.getenv("RISK_REDUCE_PCT", "50") or 50),
        "risk_reduce_buffer_pct": float(os.getenv("RISK_REDUCE_BUFFER_PCT", "0.8") or 0.8),
        "risk_full_buffer_pct": float(os.getenv("RISK_FULL_BUFFER_PCT", "4") or 4),
        "risk_reduce_ideal_near": float(os.getenv("RISK_REDUCE_IDEAL_NEAR", "90") or 90),
    }


def format_dca_hint(args: argparse.Namespace | None = None) -> str:
    """Display hint with the flags this watch would pass to `dca`."""
    s = stack_params(args)
    exit_mode = s.get("trade_exit", "structure")
    be = " --protect-be" if s.get("protect_be", True) else " --no-protect-be"
    if exit_mode == "ob":
        return (
            f"Hint: dca SYMBOL short --exit ob{be} "
            f"--imb-long {s['imb_long']:g} "
            f"--min-gap {s['min_gap']:g} --so-count {s['so_count']}"
        )
    if exit_mode == "trailing":
        return (
            f"Hint: dca SYMBOL short --exit trailing{be} "
            f"--min-gap {s['min_gap']:g} --so-count {s['so_count']}"
        )
    if exit_mode == "pullback":
        return (
            f"Hint: dca SYMBOL short --exit pullback{be} "
            f"--min-gap {s['min_gap']:g} --so-count {s['so_count']}"
        )
    if exit_mode == "ratchet":
        return (
            f"Hint: dca SYMBOL short --exit ratchet "
            f"--min-gap {s['min_gap']:g} --so-count {s['so_count']}"
        )
    return (
        f"Hint: dca SYMBOL short --exit structure{be} "
        f"--post-be trail --post-be-arm-pct {s['post_be_arm_pct']:g} "
        f"--post-be-callback {s['post_be_callback']:g} "
        f"--min-gap {s['min_gap']:g} --so-count {s['so_count']}"
    )


def build_snapshot(
    *,
    hits: list[PumpStallHit],
    blocked: list[AnalyzeRow],
    ideal_near: float,
    cycle: int,
    time_s: str,
    scan_s: float,
    next_s: float,
    mode: str,
    why_limit: int,
    hint: str | None = None,
    stack: dict | None = None,
) -> dict:
    """Payload for Pumpstall web (same keys as DemoScanSnapshot)."""
    ranked = sorted(
        hits,
        key=lambda h: (0 if h.near_high_pct >= ideal_near else 1, -h.score),
    )
    counts: dict[str, int] = {}
    for r in blocked:
        key = r.blocked_by or "?"
        counts[key] = counts.get(key, 0) + 1
    block_counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    show_n = max(0, int(why_limit))
    return {
        "cycle": int(cycle),
        "time": time_s,
        "scan_s": round(float(scan_s), 1),
        "next_s": float(next_s),
        "mode": mode,
        "ideal_near": float(ideal_near),
        "hits": [_hit_to_dict(h) for h in ranked],
        "blocked": [_blocked_to_dict(r) for r in blocked[:show_n]],
        "block_counts": block_counts,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hint": hint or format_dca_hint(),
        "stack": stack or stack_params(),
    }


def write_snapshot(path: Path | str, payload: dict) -> None:
    """Atomic JSON write. Never raises into the trading loop."""
    try:
        dest = Path(path)
        if not dest.is_absolute():
            dest = ROOT / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001
        print(f"{DIM}snapshot write skipped: {exc}{RESET}", flush=True)


def maybe_write_snapshot(
    args: argparse.Namespace,
    *,
    hits: list[PumpStallHit],
    blocked: list[AnalyzeRow],
    cycle: int,
    time_s: str,
    scan_s: float,
    mode: str,
) -> None:
    if getattr(args, "no_snapshot", False):
        return
    path = getattr(args, "snapshot", None)
    if not path:
        return
    payload = build_snapshot(
        hits=hits,
        blocked=blocked,
        ideal_near=float(args.ideal_near),
        cycle=cycle,
        time_s=time_s,
        scan_s=scan_s,
        next_s=float(getattr(args, "interval", 60.0) or 60.0),
        mode=mode,
        why_limit=int(getattr(args, "why", 15) or 0),
        hint=format_dca_hint(args),
        stack=stack_params(args),
    )
    write_snapshot(path, payload)


def print_blocked(blocked: list[AnalyzeRow], *, limit: int) -> None:
    """Show seed symbols that failed, colored by first failing filter."""
    if limit <= 0:
        return
    show = blocked[:limit]
    if not show:
        print(f"{DIM}No blocked seed rows to show.{RESET}")
        return
    counts: dict[str, int] = {}
    for r in blocked:
        counts[r.blocked_by or "?"] = counts.get(r.blocked_by or "?", 0) + 1
    legend = " · ".join(
        f"{_block_tag(k)}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    print(f"{DIM}Blocked (first fail) · showing {len(show)}/{len(blocked)} · {RESET}{legend}")
    print(
        f"{DIM}{'':>1} {'#':>2}  {'SYMBOL':<14} {'24h%':>7} {'pump':>5} "
        f"{'near':>5} {'reg%':>5} {'shp':>4} {'stl':>4} {'w':>3}  why{RESET}"
    )
    for i, r in enumerate(show, 1):
        chg_c = GREEN if r.change_24h >= 0 else RED
        why = r.blocked_by or "?"
        print(
            f"  {i:>2}  {CYAN}{r.symbol:<14}{RESET} "
            f"{chg_c}{r.change_24h:>+6.1f}%{RESET} "
            f"{r.pump_7d_pct:>4.0f}% "
            f"{r.near_high_pct:>4.0f}% "
            f"{r.near_regime_pct:>4.0f}% "
            f"{r.sharp_pct:>3.0f} "
            f"{r.stall_score:>3.0f} "
            f"{r.ask_walls:>3}  "
            f"{_block_tag(why)} {DIM}{r.detail}{RESET}"
        )
    print()


def print_hits(
    hits: list[PumpStallHit],
    *,
    ideal_near: float,
    prev: dict[str, float] | None = None,
    blocked: list[AnalyzeRow] | None = None,
    why_limit: int = 0,
    hint: str | None = None,
) -> dict[str, float]:
    """Render table. Returns symbol→score map for the next refresh diff."""
    if not hits:
        print(f"{YELLOW}No pump→stall short-grid candidates right now.{RESET}")
    else:
        ranked = sorted(
            hits,
            key=lambda h: (0 if h.near_high_pct >= ideal_near else 1, -h.score),
        )
        print(
            f"{DIM}★ = ideal (≤{100 - ideal_near:.0f}% off 1D top · near≥{ideal_near:.0f}%)"
            f" · blank = late · + = new this refresh{RESET}"
        )
        print(
            f"{BOLD}{'':>1} {'#':>2}  {'SYMBOL':<14} {'24h%':>7} {'pump':>7} "
            f"{'near':>5} {'reg%':>5} {'shp':>4} {'stl':>4} "
            f"{'w':>3} {'span':>5}  score  note{RESET}"
        )
        now_map: dict[str, float] = {}
        for i, h in enumerate(ranked, 1):
            now_map[h.symbol] = h.score
            star = "★" if h.near_high_pct >= ideal_near else " "
            flag = " "
            if prev is not None and h.symbol not in prev:
                flag = "+"
            elif prev is not None and h.symbol in prev:
                d = h.score - prev[h.symbol]
                if d >= 3:
                    flag = "↑"
                elif d <= -3:
                    flag = "↓"
            chg_c = GREEN if h.change_24h >= 0 else RED
            star_s = f"{YELLOW}{star}{RESET}" if star.strip() else " "
            flag_s = (
                f"{GREEN}{flag}{RESET}" if flag in "+↑"
                else (f"{RED}{flag}{RESET}" if flag == "↓" else " ")
            )
            print(
                f"{star_s}{flag_s} {i:>2}  {CYAN}{h.symbol:<14}{RESET} "
                f"{chg_c}{h.change_24h:>+6.1f}%{RESET} "
                f"{h.pump_7d_pct:>6.0f}% "
                f"{h.near_high_pct:>4.0f}% "
                f"{h.near_regime_pct:>4.0f}% "
                f"{h.sharp_pct:>3.0f} "
                f"{h.stall_score:>3.0f} "
                f"{h.ask_walls:>3} "
                f"{h.ask_span_pct:>4.1f}  "
                f"{h.score:>5.1f}  {DIM}{h.note}{RESET}"
            )
            if h.wall_prices:
                px = " → ".join(f"{p:g}" for p in h.wall_prices[:6])
                print(f"      {DIM}ask walls: {px}{RESET}")
        if prev is not None:
            gone = [s for s in prev if s not in now_map]
            if gone:
                print(f"{DIM}left: {', '.join(gone)}{RESET}")
        print()
        print(f"{DIM}{hint or format_dca_hint()}{RESET}")
        if why_limit > 0 and blocked is not None:
            print()
            print_blocked(blocked, limit=why_limit)
        return now_map

    if why_limit > 0 and blocked is not None:
        print()
        print_blocked(blocked, limit=why_limit)
    elif why_limit > 0:
        print(f"{DIM}(no blocked rows){RESET}")
    return {}

def _maybe_telegram_notify(
    hits: list[PumpStallHit],
    *,
    ideal_near: float,
    prev_map: dict[str, float] | None,
    enabled: bool,
    bootstrap: bool = False,
) -> None:
    """Post ★ candidates (setup only — not #OPEN) to the public Pumpstall channel."""
    if not enabled:
        return
    try:
        import pumpstall_telegram as pst
    except ImportError:
        print(f"{DIM}Telegram: pumpstall_telegram not available{RESET}")
        return
    if not pst.is_configured():
        print(f"{DIM}Telegram: Pumpstall channel not configured — skip{RESET}")
        return
    if prev_map is None and not bootstrap:
        return

    sent = 0
    for h in hits:
        if h.near_high_pct < ideal_near:
            continue
        if prev_map is not None and h.symbol in prev_map and not bootstrap:
            continue
        if pst.notify_open_hit(h):
            sent += 1
            print(f"{GREEN}Telegram ★ {h.symbol}{RESET}")
        else:
            print(f"{YELLOW}Telegram failed {h.symbol}{RESET}")
    if sent:
        print(f"{DIM}Telegram: posted {sent} ★ setup(s){RESET}")
    elif bootstrap:
        print(f"{DIM}Telegram: no ★ ideals to post{RESET}")


def _maybe_daily_orphan_sweep() -> None:
    """Cancel bot limit/algo orphans on flat symbols (same cadence as #REPORT)."""
    try:
        import botctl
    except ImportError as exc:
        print(f"{DIM}Daily sweep skipped: botctl unavailable ({exc}){RESET}")
        return
    try:
        result = botctl.sweep(None)
    except Exception as exc:  # noqa: BLE001
        print(f"{YELLOW}Daily orphan sweep failed: {exc}{RESET}")
        return
    for line in str(result).splitlines():
        print(f"{DIM}Sweep: {line}{RESET}")


def _maybe_daily_summary(enabled: bool) -> None:
    if not enabled:
        return
    try:
        import pumpstall_telegram as pst
    except ImportError:
        return
    if pst.maybe_send_daily_summary():
        print(f"{GREEN}Telegram: daily PnL summary sent{RESET}")
        _maybe_daily_orphan_sweep()


def _clear_screen() -> None:
    # Keep scrollback usable; full clear each refresh
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _account_open_symbols(recv_window: int = 15000) -> list[str]:
    """Symbols with non-zero futures position (needs API keys)."""
    from orderbook_dca_grid import _signed_request, load_keys

    api, sec = load_keys(None)
    if not api or not sec:
        return []
    try:
        rows = _signed_request("GET", "/fapi/v2/positionRisk", {}, api, sec, recv_window)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for r in rows:
        try:
            amt = abs(float(r.get("positionAmt") or 0))
        except (TypeError, ValueError):
            continue
        if amt > 0:
            sym = str(r.get("symbol") or "").upper()
            if sym:
                out.append(sym)
    return sorted(set(out))


def _dca_supervisor_running() -> list[str]:
    """Symbols with a live orderbook_dca_grid.py --supervise process."""
    try:
        from botctl import _pgrep_supervisors

        return list(_pgrep_supervisors())
    except Exception:
        return []


def _pick_ideals(
    hits: list[PumpStallHit],
    ideal_near: float,
    *,
    exclude: set[str],
    limit: int,
) -> list[PumpStallHit]:
    """Top ★ ideals by score, skipping symbols already busy."""
    if limit <= 0:
        return []
    ideals = [
        h for h in hits
        if h.near_high_pct >= ideal_near and h.symbol.upper() not in exclude
    ]
    ideals.sort(key=lambda h: h.score, reverse=True)
    return ideals[:limit]


def _reap_active(
    active: dict[str, subprocess.Popen],
) -> dict[str, subprocess.Popen]:
    """Drop finished --once children; log exits."""
    alive: dict[str, subprocess.Popen] = {}
    for sym, proc in active.items():
        code = proc.poll()
        if code is None:
            alive[sym] = proc
            continue
        print(
            f"{DIM}AUTO: {sym} --once exited (code {code}) — slot free{RESET}"
        )
    return alive


def _weekend_block_active() -> bool:
    """True during Fri 21:00 UTC → Sun 23:00 UTC (no new ★).

    Override with PUMPSTALL_WEEKEND_BLOCK=0 to disable (default: on).
    """
    flag = (os.getenv("PUMPSTALL_WEEKEND_BLOCK", "1") or "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    now = datetime.now(timezone.utc)
    wd = now.weekday()  # Mon=0 … Sun=6
    hm = (now.hour, now.minute)
    if wd == 4 and hm >= (21, 0):  # Friday after 21:00 UTC
        return True
    if wd == 5:  # Saturday
        return True
    if wd == 6 and hm < (23, 0):  # Sunday before 23:00 UTC
        return True
    return False


def _trade_exit_mode(args: argparse.Namespace) -> str:
    """Primary exit for auto-trade children.

    Preference: TRADE_EXIT / EXIT_MODE in .env (EnvironmentFile) → --trade-exit → structure.
    So a VPS .env change wins over a hardcoded unit flag after restart.
    """
    raw = (
        os.getenv("TRADE_EXIT")
        or os.getenv("EXIT_MODE")
        or getattr(args, "trade_exit", None)
        or "structure"
    )
    raw = str(raw).strip().lower()
    if raw in ("be-ob", "be_ob", "beob", "ob-long", "ob_long", "oblong", "ob"):
        return "ob"
    if raw in ("trailing", "trail"):
        return "trailing"
    if raw in ("pullback", "pb", "pull", "giveback"):
        return "pullback"
    if raw in ("ratchet", "support-be", "support_be", "ratchet-be", "ratchet_be", "levels"):
        return "ratchet"
    if raw in ("eql", "eq", "structure"):
        return "structure"
    if raw in ("be", "staged", "none"):
        return raw
    return "structure"


def _protect_be_for_trade(args: argparse.Namespace) -> bool:
    """Whether child dca gets --protect-be (default on)."""
    return bool(getattr(args, "protect_be", True))


def _max_trades(args: argparse.Namespace) -> int:
    """Concurrent ★ slots for auto-trade.

    Preference: MAX_TRADES in .env → --max-trades → 3.
    """
    raw = os.getenv("MAX_TRADES")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            pass
    try:
        return max(1, int(getattr(args, "max_trades", 3) or 3))
    except (TypeError, ValueError):
        return 3


def _launch_dca_once(hit: PumpStallHit, args: argparse.Namespace) -> subprocess.Popen | None:
    """Start `dca SYMBOL short` with the configured trade exit --once."""
    root = _repo_root()
    dca_bin = os.path.join(root, "dca")
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"pump-stall-{hit.symbol}.log")
    exit_mode = _trade_exit_mode(args)
    use_be = _protect_be_for_trade(args)
    cmd = [
        dca_bin,
        hit.symbol,
        "short",
        "--exit", exit_mode,
        "--protect-be" if use_be else "--no-protect-be",
        "--once",
        "--loss-cooldown-min", str(getattr(args, "loss_cooldown_min", 1440)),
        "--margin-ratio-soft", str(getattr(args, "margin_ratio_soft", 5.0)),
        "--margin-ratio-hard", str(getattr(args, "margin_ratio_hard", 8.0)),
        "--so-count", str(args.so_count),
        "--min-gap", str(args.min_gap),
        "--min-dist", str(args.min_dist),
        "--max-range", str(args.max_range),
        "--limit", str(args.limit),
    ]
    if use_be:
        cmd.extend(["--be-arm-pct", "1", "--be-profit-pct", "0.3"])
    be_note = " + BE@1%→0.3%" if use_be else ""
    if exit_mode == "structure":
        cmd.extend([
            "--partial-tp",
            "--tp-partial-pct", "70",
            "--tp1-profit-pct", "0.3",
            "--partial-tp-min-entry-pct", "500",
        ])
        # post-BE trail only when BE is on and caller asked for it
        if use_be and float(getattr(args, "post_be_arm_pct", 0) or 0) > 0:
            cmd.extend([
                "--post-be", "trail",
                "--post-be-arm-pct", str(getattr(args, "post_be_arm_pct", 1.5) or 1.5),
                "--post-be-callback", str(getattr(args, "post_be_callback", 0.6) or 0.6),
            ])
            trail_note = (
                f" + post-BE trail@"
                f"{float(getattr(args, 'post_be_arm_pct', 1.5) or 1.5):g}%/"
                f"{float(getattr(args, 'post_be_callback', 0.6) or 0.6):g}%"
            )
        else:
            trail_note = ""
        if getattr(args, "structure_interval", None):
            cmd.extend(["--structure-interval", str(args.structure_interval)])
        launch_note = (
            f"dca short --exit structure{be_note}{trail_note} "
            f"+ TP70%@+0.3%(≥500% entry) --once"
        )
    elif exit_mode == "trailing":
        launch_note = f"dca short --exit trailing{be_note} --once"
    elif exit_mode == "pullback":
        launch_note = f"dca short --exit pullback{be_note} --once"
    elif exit_mode == "ratchet":
        # Ratchet owns the BE algo tag — do not stack classic protect-be
        if "--protect-be" in cmd:
            cmd[cmd.index("--protect-be")] = "--no-protect-be"
        elif "--no-protect-be" not in cmd:
            cmd.append("--no-protect-be")
        # Drop BE arm flags if we already appended them
        for flag in ("--be-arm-pct", "--be-profit-pct"):
            if flag in cmd:
                i = cmd.index(flag)
                del cmd[i:i + 2]
        launch_note = "dca short --exit ratchet --once"
    elif exit_mode == "ob":
        imb = getattr(args, "imb_long", None)
        if imb is not None:
            cmd.extend(["--imb-long", str(imb)])
        band = getattr(args, "ob_band_pct", None)
        if band is not None:
            cmd.extend(["--ob-band-pct", str(band)])
        launch_note = (
            f"dca short --exit ob{be_note} · OB Long "
            f"(imb≥{float(imb if imb is not None else 0.55):g}) --once"
        )
    else:
        launch_note = f"dca short --exit {exit_mode}{be_note} --once"
    try:
        log_f = open(log_path, "a", encoding="utf-8")
        log_f.write(
            f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"score={hit.score:.1f} near={hit.near_high_pct:.0f}% "
            f"exit={exit_mode} ---\n"
        )
        log_f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(
            f"{BOLD}{GREEN}AUTO ★ {hit.symbol}{RESET}  "
            f"{DIM}pid={proc.pid} · {launch_note} · log {log_path}{RESET}"
        )
        return proc
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}Failed to launch dca for {hit.symbol}: {exc}{RESET}")
        return None


def _maybe_auto_trade(
    hits: list[PumpStallHit],
    args: argparse.Namespace,
    *,
    active: dict[str, subprocess.Popen],
) -> dict[str, subprocess.Popen]:
    """Keep the top --max-trades ★ from this scan running (this bot only).

    Other account positions / unrelated supervisors do not consume slots.
    We never launch outside the current top-N ★ list.
    """
    active = _reap_active(active)
    max_trades = _max_trades(args)

    # Size boost each cycle (even if weekend/margin blocks new ★ launches)
    try:
        import size_boost as sb

        boost_note = sb.sync_auto_boost(hits, ideal_near=float(args.ideal_near))
        if boost_note:
            print(f"{BOLD}{CYAN}{boost_note}{RESET}")
    except Exception as exc:  # noqa: BLE001
        print(f"{DIM}AUTO boost skipped: {exc}{RESET}")

    if _weekend_block_active():
        print(
            f"{YELLOW}AUTO: weekend block Fri 21:00→Sun 23:00 UTC "
            f"— no new ★{RESET}"
        )
        return active

    import loss_cooldown as lcd
    from orderbook_dca_grid import get_margin_ratio_pct, load_keys

    soft_mr = float(getattr(args, "margin_ratio_soft", 5.0) or 0)
    if soft_mr > 0:
        api, sec = load_keys(None)
        if api and sec:
            ratio = get_margin_ratio_pct(api, sec, 15000)
            if ratio is not None and ratio >= soft_mr:
                print(
                    f"{YELLOW}AUTO: margin ratio {ratio:.2f}% ≥ soft "
                    f"{soft_mr:g}% — no new ★ "
                    f"(DCA strip at hard "
                    f"{getattr(args, 'margin_ratio_hard', 8):g}%){RESET}"
                )
                return active

    cooling = lcd.cooling_map()
    if cooling:
        bits = [f"{s} {lcd.fmt_remaining(t)}" for s, t in sorted(cooling.items())]
        print(f"{DIM}AUTO: loss cooldown · {', '.join(bits)}{RESET}")

    # Target set: first N ★ by score, skipping symbols in loss cooldown
    target = _pick_ideals(
        hits, args.ideal_near, exclude=set(cooling), limit=max_trades,
    )
    target_syms = [h.symbol.upper() for h in target]
    if not target_syms:
        if cooling:
            print(f"{DIM}AUTO: no ★ ideal outside cooldown — skip{RESET}")
        else:
            print(f"{DIM}AUTO: no ★ ideal this round — skip{RESET}")
        return active

    running = {s.upper() for s in _dca_supervisor_running()}
    ours = {s.upper() for s in active}

    print(
        f"{DIM}AUTO: target ★ top-{max_trades}: {', '.join(target_syms)}"
        f" · ours {', '.join(sorted(ours)) or '—'} · "
        f"supervise {', '.join(sorted(running)) or '—'}{RESET}"
    )

    for hit in target:
        sym = hit.symbol.upper()
        if sym in ours or sym in running:
            continue  # already covered (ours or any supervise on this symbol)
        if len(active) >= max_trades:
            print(
                f"{DIM}AUTO: at --max-trades={max_trades} "
                f"(this bot) — wait for a slot{RESET}"
            )
            break
        proc = _launch_dca_once(hit, args)
        if proc is not None:
            active[sym] = proc
            ours.add(sym)

    missing = [s for s in target_syms if s not in ours and s not in running]
    covered = [s for s in target_syms if s in ours or s in running]
    if covered and not missing:
        print(f"{DIM}AUTO: top ★ covered ({', '.join(covered)}){RESET}")
    elif missing and len(active) >= max_trades:
        pass  # already logged slot wait
    elif missing:
        print(f"{DIM}AUTO: still need {', '.join(missing)}{RESET}")

    return active


def watch_loop(args: argparse.Namespace) -> int:
    """Live supervisor: rescan on an interval and redraw the table."""
    interval = max(15.0, float(args.interval))
    prev: dict[str, float] | None = None
    round_n = 0
    active: dict[str, subprocess.Popen] = {}
    auto = bool(getattr(args, "auto_trade", False))
    max_trades = _max_trades(args)
    mode = (
        f"AUTO-TRADE · --once · top {max_trades} ★"
        if auto else "display only"
    )
    print(
        f"{BOLD}{CYAN}Pump→stall supervisor{RESET}  "
        f"{DIM}{mode} · refresh every {interval:g}s · Ctrl+C to stop{RESET}"
    )
    try:
        while True:
            round_n += 1
            t0 = time.time()
            try:
                hits, blocked = scan(args)
            except Exception as exc:  # noqa: BLE001
                _clear_screen()
                print(f"{RED}Scan failed: {exc}{RESET}")
                print(f"{DIM}Retrying in {interval:g}s…{RESET}")
                time.sleep(interval)
                continue
            elapsed = time.time() - t0
            _clear_screen()
            now = time.strftime("%H:%M:%S")
            print(
                f"{BOLD}{CYAN}Pump→stall supervisor{RESET}  "
                f"{DIM}#{round_n} · {now} · scan {elapsed:.1f}s · "
                f"next in {interval:g}s · {mode} · Ctrl+C{RESET}"
            )
            print()
            why_n = int(getattr(args, "why", 15) or 0)
            prev_before = prev
            prev = print_hits(
                hits,
                ideal_near=args.ideal_near,
                prev=prev,
                blocked=blocked,
                why_limit=why_n,
                hint=format_dca_hint(args),
            )
            tg_on = bool(getattr(args, "telegram", False))
            _maybe_telegram_notify(
                hits,
                ideal_near=args.ideal_near,
                prev_map=prev_before,
                enabled=tg_on,
            )
            _maybe_daily_summary(tg_on)
            maybe_write_snapshot(
                args,
                hits=hits,
                blocked=blocked,
                cycle=round_n,
                time_s=now,
                scan_s=elapsed,
                mode=mode,
            )
            if auto:
                print()
                active = _maybe_auto_trade(hits, args, active=active)
            left = interval
            while left > 0:
                step = min(1.0, left)
                time.sleep(step)
                left -= step
    except KeyboardInterrupt:
        print(f"\n{DIM}supervisor stopped.{RESET}")
        still = [f"{s} pid={p.pid}" for s, p in active.items() if p.poll() is None]
        if still:
            print(
                f"{DIM}Note: dca --once still running "
                f"({', '.join(still)}) — not killed.{RESET}"
            )
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Scan / supervise 1D blow-off → stall shorts with ask liquidity. "
            "Display-only by default; --watch --auto-trade places real orders."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./pump-stall                              # one-shot table (no orders)
  ./pump-stall-watch                        # live table (no orders)
  ./pump-stall --why 15                     # show why seeds were blocked
  ./pump-stall-watch --auto-trade           # top 3 ★ → dca short --once
  ./pump-stall-watch-early                  # TEST: looser filters + auto-trade

Auto-trade launches per ★:
  dca SYMBOL short --exit structure --protect-be \\
    --be-arm-pct 1 --be-profit-pct 0.3 \\
    --post-be trail --post-be-arm-pct 1.5 --post-be-callback 0.6 --once

  TP = EQL (short) / EQH (long).
  BE protect arms at +1% → SL @ entry+0.3%; trail from +2% (cb 0.8%).
  Loss close → --loss-cooldown-min on that symbol (default 1440 = 24h).
  Slots = top ★ of this list only (other account pairs ignored).

Production (VPS):
  sudo systemctl enable --now pump-stall-watch
  sudo journalctl -u pump-stall-watch -f
  See README.md → Pump→stall scanner.
""",
    )
    p.add_argument("--base", default=FAPI_BASE, help="Futures REST base URL")
    p.add_argument("--top", type=int, default=12, help="Max hits to show")
    p.add_argument("--min-quote-vol", type=float, default=5_000_000.0,
                   help="Min 24h quote volume USDT")
    p.add_argument("--min-change-24h", type=float, default=8.0,
                   help="Seed pool: include symbols with 24h %% ≥ this")
    p.add_argument("--pool-gainers", type=int, default=40,
                   help="Top N 24h gainers to seed")
    p.add_argument("--pool-volume", type=int, default=30,
                   help="Top N by volume (if also rising) to seed")
    p.add_argument("--min-pump-7d", type=float, default=35.0,
                   help="Min trough→peak %% over recent ~14d")
    p.add_argument("--min-near-high", type=float, default=85.0,
                   help="Min last/recent-peak %% to appear in the table")
    p.add_argument("--ideal-near", type=float, default=92.0,
                   help="near%% ≥ this → ★ ideal short zone (default 92 ≈ ≤8%% off top)")
    p.add_argument("--min-near-regime", type=float, default=80.0,
                   help="Min last/30–45d high %% — rejects downtrend bounces (ZBT)")
    p.add_argument("--min-sharp", type=float, default=35.0,
                   help="Min %% of pump concentrated in last ~5 daily bars")
    p.add_argument("--require-recent-peak", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="1D regime high must be in the last ~7 bars (default on)")
    p.add_argument("--min-stall", type=float, default=35.0,
                   help="Min stall score 0..100")
    p.add_argument("--min-walls", type=int, default=3,
                   help="Min ask DCA walls for short grid")
    p.add_argument("--so-count", type=int, default=8)
    p.add_argument("--min-gap", type=float, default=0.8)
    p.add_argument("--min-dist", type=float, default=0.1)
    p.add_argument("--max-range", type=float, default=12.0)
    p.add_argument("--limit", type=int, default=500, help="Depth limit")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument(
        "--watch",
        action="store_true",
        help="Live supervisor: refresh table on screen (Ctrl+C to stop)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Watch refresh interval in seconds (default 60, min 15)",
    )
    p.add_argument(
        "--auto-trade",
        action="store_true",
        help="With --watch: run the top ★ from this list via "
             "`dca SYMBOL short` + --trade-exit --once (up to --max-trades)",
    )
    p.add_argument(
        "--trade-exit",
        choices=["structure", "ob", "trailing", "pullback", "ratchet", "be", "staged", "none"],
        default="structure",
        help="Primary exit for auto-trade children. .env TRADE_EXIT / EXIT_MODE "
             "overrides unit flags after restart. Default: structure",
    )
    p.add_argument(
        "--protect-be",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass --protect-be to dca children (default on). "
             "Use --no-protect-be for exit-only (no BE SL)",
    )
    p.add_argument(
        "--imb-long",
        type=float,
        default=None,
        help="With --trade-exit ob: OB Long imbalance threshold (default 0.55)",
    )
    p.add_argument(
        "--ob-band-pct",
        type=float,
        default=None,
        help="With --trade-exit ob: depth band %% for imbalance (default 1.0)",
    )
    p.add_argument(
        "--max-trades",
        type=int,
        default=3,
        help="With --auto-trade: how many top ★ from this scan to run "
             "(this bot only; other account pairs do not count; default 3). "
             ".env MAX_TRADES overrides after restart",
    )
    p.add_argument(
        "--loss-cooldown-min",
        type=float,
        default=1440.0,
        help="With --auto-trade: after a losing close, skip that symbol for N minutes "
             "(passed to dca; default 1440 = 24h, 0=off)",
    )
    p.add_argument(
        "--structure-interval",
        default=None,
        help="Passed to dca --structure-interval when auto-trading",
    )
    p.add_argument(
        "--post-be-arm-pct",
        type=float,
        default=1.5,
        help="With --auto-trade: arm post-BE trail at this profit %% (default 1.5)",
    )
    p.add_argument(
        "--post-be-callback",
        type=float,
        default=0.6,
        help="With --auto-trade: post-BE trailing callbackRate %% (default 0.6)",
    )
    p.add_argument(
        "--margin-ratio-soft",
        type=float,
        default=float(os.getenv("MARGIN_RATIO_SOFT", "5") or 5),
        help="Binance Margin Ratio %% ≥ this → no new ★ (default 5; 0=off). "
             "Env: MARGIN_RATIO_SOFT",
    )
    p.add_argument(
        "--margin-ratio-hard",
        type=float,
        default=float(os.getenv("MARGIN_RATIO_HARD", "8") or 8),
        help="Passed to dca: %% ≥ this → cancel DCA limits; below → re-arm "
             "(default 8; 0=off). Env: MARGIN_RATIO_HARD",
    )
    p.add_argument(
        "--why",
        type=int,
        nargs="?",
        const=15,
        default=15,
        metavar="N",
        help="Show top N blocked seed symbols colored by first failing filter "
             "(default 15; --why 0 to hide; --why 30 for more)",
    )
    p.add_argument(
        "--snapshot",
        default=str(DEFAULT_SNAPSHOT),
        help="Write JSON snapshot for Pumpstall web after each scan "
             f"(default: {DEFAULT_SNAPSHOT})",
    )
    p.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Do not write the web snapshot JSON (trading unchanged either way)",
    )
    p.add_argument(
        "--telegram",
        action="store_true",
        help="Post ★ opens + daily PnL %% summary to the public Pumpstall channel "
             "(TELEGRAM_BOT_TOKEN + TELEGRAM_PUMPSTALL_CHAT_ID=@pumpstall). "
             "Closes with %% only are mirrored whenever the channel is configured.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.auto_trade and not args.watch:
        print(f"{YELLOW}--auto-trade requires --watch{RESET}", file=sys.stderr)
        return 2
    if args.watch:
        return watch_loop(args)
    t0 = time.time()
    print(
        f"{BOLD}{CYAN}Pump→stall short-grid scan{RESET}  "
        f"{DIM}(1D blow-off filter · display only){RESET}"
    )
    hits, blocked = scan(args)
    print_hits(
        hits,
        ideal_near=args.ideal_near,
        blocked=blocked,
        why_limit=int(getattr(args, "why", 15) or 0),
        hint=format_dca_hint(args),
    )
    if getattr(args, "telegram", False):
        _maybe_telegram_notify(
            hits,
            ideal_near=args.ideal_near,
            prev_map=None,
            enabled=True,
            bootstrap=True,
        )
    maybe_write_snapshot(
        args,
        hits=hits,
        blocked=blocked,
        cycle=1,
        time_s=time.strftime("%H:%M:%S"),
        scan_s=time.time() - t0,
        mode="display only",
    )
    print(f"{DIM}done in {time.time() - t0:.1f}s{RESET}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
