#!/usr/bin/env python3
"""Detect pump → stall → short-grid candidates on Binance USDT-M perps.

1D structure filter (same judgment as reading the daily chart):
  1. Large recent trough→peak pump
  2. Still near that peak AND near the ~30–45d regime high (rejects downtrend bounces)
  3. Move is sharp / recent (blow-off), not a slow grind
  4. Starting to stall on daily ranges
  5. Enough ask-side order-book walls for a SHORT DCA grid

Display only — does not place orders.

  python3 pump_stall_scan.py
  ./pump-stall --top 15 --min-near-regime 80 --min-sharp 35
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
from dataclasses import dataclass

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
) -> PumpStallHit | None:
    try:
        # ~45 daily bars: enough to see downtrend vs blow-off at the right edge
        kl = fetch_klines(base, t.symbol, "1d", 45)
    except Exception:
        return None
    daily = _ohlc(kl)
    m = _day_metrics(daily)
    if m is None:
        return None
    if m.pump_pct < min_pump_7d:
        return None
    if m.near_high_pct < min_near_high:
        return None
    # Reject "bounce in a larger downtrend" (ZBT): still far below 1D regime high
    if m.near_regime_pct < min_near_regime:
        return None
    if m.sharp_pct < min_sharp:
        return None
    if require_recent_peak and not m.peak_is_recent:
        return None
    if m.stall_score < min_stall:
        return None

    walls_n, span, prices = _ask_grid(
        t.symbol,
        so_count=so_count,
        min_gap=min_gap,
        min_dist=min_dist,
        max_range=max_range,
        limit=depth_limit,
    )
    if walls_n < min_walls:
        return None

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
    return PumpStallHit(
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


def scan(args: argparse.Namespace) -> list[PumpStallHit]:
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

    hits: list[PumpStallHit] = []
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
                hit = fut.result()
            except Exception:
                hit = None
            if hit:
                hits.append(hit)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[: args.top]


def print_hits(hits: list[PumpStallHit], *, ideal_near: float) -> None:
    if not hits:
        print(f"{YELLOW}No pump→stall short-grid candidates right now.{RESET}")
        return
    # Ideals first (near top), then the rest — both stay in the table
    ranked = sorted(
        hits,
        key=lambda h: (0 if h.near_high_pct >= ideal_near else 1, -h.score),
    )
    print()
    print(
        f"{DIM}★ = ideal short zone (≤{100 - ideal_near:.0f}% off 1D top · near≥{ideal_near:.0f}%)"
        f" · blank = listed but late / farther from top{RESET}"
    )
    print(
        f"{BOLD}{'':>1} {'#':>2}  {'SYMBOL':<14} {'24h%':>7} {'pump':>7} "
        f"{'near':>5} {'reg%':>5} {'shp':>4} {'stl':>4} "
        f"{'w':>3} {'span':>5}  score  note{RESET}"
    )
    for i, h in enumerate(ranked, 1):
        star = f"{YELLOW}★{RESET}" if h.near_high_pct >= ideal_near else " "
        chg_c = GREEN if h.change_24h >= 0 else RED
        print(
            f"{star} {i:>2}  {CYAN}{h.symbol:<14}{RESET} "
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
    print()
    print(
        f"{DIM}Hint: dca SYMBOL short --exit structure "
        f"--min-gap … --so-count …{RESET}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan for 1D blow-off → stall shorts with ask liquidity for a DCA grid"
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    print(
        f"{BOLD}{CYAN}Pump→stall short-grid scan{RESET}  "
        f"{DIM}(1D blow-off filter · display only){RESET}"
    )
    hits = scan(args)
    print_hits(hits, ideal_near=args.ideal_near)
    print(f"{DIM}done in {time.time() - t0:.1f}s{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
