#!/usr/bin/env python3
"""Scan futures movers, score order-book grids, ask DeepSeek for one trade pick."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from futures_scan import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    FAPI_BASE,
    SymbolInsight,
    TickerRow,
    build_insight,
    fetch_tickers,
    trading_usdt_perps,
)


def parse_pairs(raw: str) -> set[str]:
    out: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        sym = part.strip().upper()
        if sym:
            out.add(sym)
    return out


def fleet_pairs_to_skip(*, include_fleet: bool) -> set[str]:
    """Exclude FUTURES_PAIRS — symbols already on the VPS supervisor fleet."""
    if include_fleet:
        return set()
    return parse_pairs(os.getenv("FUTURES_PAIRS", ""))


def apply_skip(tickers: list[TickerRow], skip: set[str]) -> list[TickerRow]:
    if not skip:
        return tickers
    return [t for t in tickers if t.symbol not in skip]

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"


@dataclass
class GridSnapshot:
    auto_direction: str
    bid_share_pct: float
    dca_walls: int
    dca_target: int
    entry_usdt: float
    grid_notional_usdt: float
    blocked_by_imbalance: bool
    wall_prices: list[float]


@dataclass
class Candidate:
    ticker: TickerRow
    insight: SymbolInsight
    grid: GridSnapshot
    local_score: float
    score_breakdown: dict[str, float]


def _grid_args(symbol: str) -> argparse.Namespace:
    from orderbook_dca_grid import _env_bool, _env_float

    return argparse.Namespace(
        symbol=symbol.upper(),
        env_file=None,
        direction="auto",
        auto_range=1.0,
        price=None,
        base_size=_env_float("BASE_SIZE", 0.0),
        wallet_pct=_env_float("WALLET_PCT", 10.0),
        max_imbalance=_env_float("MAX_IMBALANCE", 20.0),
        max_margin_pct=_env_float("MAX_MARGIN_PCT", 50.0),
        min_liq_distance_pct=_env_float("MIN_LIQ_DISTANCE_PCT", 20.0),
        max_account_notional_pct=_env_float("MAX_ACCOUNT_NOTIONAL_PCT", 80.0),
        risk_use_full_grid=_env_bool("RISK_USE_FULL_GRID", True),
        leverage=_env_float("LEVERAGE", 10.0),
        force=False,
        recv_window=int(_env_float("RECV_WINDOW", 15000)),
        position_mode="auto",
        so_count=8,
        limit=1000,
        min_gap=0.8,
        min_dist=0.1,
        max_range=12.0,
        tp=0.5,
        size_mode="comp",
        comp_factor=1.0,
        so_size=58.99,
        volume_scale=1.3,
    )


def analyze_grid(symbol: str, api: str, sec: str) -> GridSnapshot | None:
    from orderbook_dca_grid import (
        account_risk_blocks,
        build_grid,
        decide_direction,
        fetch_depth,
        get_max_leverage,
        get_wallet_balance,
        grid_add_notional,
        load_symbol_filters,
        prepare_orders,
        select_walls,
        _resolve_hedge,
    )

    args = _grid_args(symbol)
    sym = args.symbol
    try:
        filt = load_symbol_filters(sym)
        hedge = _resolve_hedge(args, api, sec)
        depth = fetch_depth(sym, args.limit)
    except Exception:
        return None

    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]
    if not bids or not asks:
        return None

    mid = (bids[0][0] + asks[0][0]) / 2
    d = decide_direction(bids, asks, mid, args.auto_range)
    is_long = d["direction"] == "long"
    direction = "LONG" if is_long else "SHORT"

    base_size = args.base_size
    if base_size <= 0:
        try:
            bal = get_wallet_balance(api, sec, args.recv_window)
            base_size = bal * args.wallet_pct / 100.0
        except Exception:
            return None

    blocked = False
    levels = bids if is_long else asks
    entry = mid
    walls = select_walls(
        levels, entry, is_long, args.so_count, args.min_gap, args.min_dist, args.max_range,
    )
    if not walls:
        try:
            lev = float(get_max_leverage(sym, api, sec, args.recv_window))
        except Exception:
            lev = float(args.leverage or 10.0)
        blocked = account_risk_blocks(
            args, is_long, base_size, api, sec, leverage=lev, verbose=False,
        )
        return GridSnapshot(
            auto_direction=direction,
            bid_share_pct=d["imbalance"] * 100,
            dca_walls=0,
            dca_target=args.so_count,
            entry_usdt=base_size,
            grid_notional_usdt=0.0,
            blocked_by_imbalance=blocked,
            wall_prices=[],
        )

    orders = build_grid(
        entry, is_long, walls, base_size, args.tp, args.size_mode,
        args.comp_factor, args.so_size, args.volume_scale,
    )
    try:
        lev = float(get_max_leverage(sym, api, sec, args.recv_window))
    except Exception:
        lev = float(args.leverage or 10.0)
    add_notional = grid_add_notional(orders, args, dca_only=False)
    blocked = account_risk_blocks(
        args, is_long, add_notional, api, sec, leverage=lev, verbose=False,
    )
    prepared = prepare_orders(orders, sym, is_long, filt)
    notional = sum(float(o["price"]) * float(o["quantity"]) for o in prepared)

    return GridSnapshot(
        auto_direction=direction,
        bid_share_pct=d["imbalance"] * 100,
        dca_walls=len(walls),
        dca_target=args.so_count,
        entry_usdt=base_size,
        grid_notional_usdt=notional,
        blocked_by_imbalance=blocked,
        wall_prices=[w[0] for w in walls[:5]],
    )


def local_score(ticker: TickerRow, insight: SymbolInsight, grid: GridSnapshot) -> tuple[float, dict[str, float]]:
    """Score for a one-off volatile trade (not fleet majors)."""
    if grid.blocked_by_imbalance:
        return 0.0, {"blocked": -999.0}

    parts: dict[str, float] = {}
    rp = ticker.range_pct
    ch = abs(ticker.change_pct)

    # Volatility is the main signal (24h range).
    if rp < 6:
        parts["volatility"] = rp * 0.5
    elif rp <= 12:
        parts["volatility"] = 15.0 + (rp - 6) * 1.5
    elif rp <= 25:
        parts["volatility"] = 24.0 + (rp - 12) * 1.8
    elif rp <= 40:
        parts["volatility"] = 47.0 - (rp - 25) * 0.8
    else:
        parts["volatility"] = 8.0

    parts["intraday"] = min(20.0, insight.vol_avg_1h * 2.5 + insight.range_1h * 0.8)

    if 4 <= ch <= 25:
        parts["move"] = min(15.0, ch * 0.6)
    elif ch > 35:
        parts["move"] = -15.0
    elif ch > 25:
        parts["move"] = -5.0
    else:
        parts["move"] = ch * 0.3

    vol = max(ticker.quote_volume, 1.0)
    parts["liquidity"] = min(12.0, max(0.0, math.log10(vol) * 4.0 - 18.0))

    trend = insight.trend.upper()
    dir_u = grid.auto_direction
    if dir_u == "LONG" and ("BULL" in trend or "LEAN LONG" in trend):
        parts["alignment"] = 8.0
    elif dir_u == "SHORT" and ("BEAR" in trend or "LEAN SHORT" in trend):
        parts["alignment"] = 8.0
    elif trend == "NEUTRAL":
        parts["alignment"] = 5.0
    else:
        parts["alignment"] = 2.0

    if grid.dca_walls == 0:
        parts["walls"] = 0.0
    else:
        parts["walls"] = min(15.0, (grid.dca_walls / max(grid.dca_target, 1)) * 15.0)

    return sum(parts.values()), parts


def quick_volatile_rank(t: TickerRow) -> float:
    """Fast pre-filter: favour range + movement + enough volume."""
    return t.range_pct * 2.0 + min(abs(t.change_pct), 30) * 0.4 + math.log10(max(t.quote_volume, 1.0))


def collect_universe_scalp(
    base: str,
    *,
    min_volume: float,
    pool_size: int,
) -> list[TickerRow]:
    """Liquid leaders for OB scalp (volume-first, not volatile alts)."""
    allowed = trading_usdt_perps(base)
    tickers = fetch_tickers(base, allowed)
    liquid = [t for t in tickers if t.quote_volume >= min_volume]
    return sorted(liquid, key=lambda t: t.quote_volume, reverse=True)[:pool_size]


def spread_bps_from_depth(symbol: str) -> float:
    from orderbook_dca_grid import fetch_depth

    depth = fetch_depth(symbol, 20)
    bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]
    if not bids or not asks:
        return 999.0
    mid = (bids[0][0] + asks[0][0]) / 2
    if mid <= 0:
        return 999.0
    return (asks[0][0] - bids[0][0]) / mid * 10_000


def scalp_score(
    ticker: TickerRow,
    insight: SymbolInsight,
    spread_bps: float,
) -> tuple[float, dict[str, float]]:
    """Score for OB scalp: liquidity + tight spread + moderate range."""
    parts: dict[str, float] = {}
    vol = max(ticker.quote_volume, 1.0)
    parts["liquidity"] = min(45.0, max(0.0, math.log10(vol) * 9.0 - 22.0))

    rp = ticker.range_pct
    if 3 <= rp <= 12:
        parts["range"] = 18.0
    elif rp <= 18:
        parts["range"] = 14.0 - (rp - 12) * 0.8
    elif rp <= 28:
        parts["range"] = max(2.0, 8.0 - (rp - 18) * 0.6)
    else:
        parts["range"] = max(0.0, 2.0 - (rp - 28) * 0.2)

    if spread_bps <= 1.5:
        parts["spread"] = 20.0
    elif spread_bps <= 3.0:
        parts["spread"] = 15.0
    elif spread_bps <= 6.0:
        parts["spread"] = 8.0
    elif spread_bps <= 12.0:
        parts["spread"] = 3.0
    else:
        parts["spread"] = 0.0

    ch = abs(ticker.change_pct)
    if ch <= 8:
        parts["stability"] = 10.0
    elif ch <= 15:
        parts["stability"] = 6.0
    elif ch <= 25:
        parts["stability"] = 2.0
    else:
        parts["stability"] = -10.0

    skew = abs(insight.book_bid_share - 50.0)
    parts["book_skew"] = min(8.0, skew * 0.15)
    parts["spread_bps"] = spread_bps
    return sum(v for k, v in parts.items() if k != "spread_bps"), parts


def enrich_scalp_candidates(
    base: str,
    tickers: list[TickerRow],
    *,
    max_analyze: int,
) -> list[Candidate]:
    out: list[Candidate] = []
    for t in tickers[: max_analyze * 2]:
        try:
            ins = build_insight(base, t.symbol, t)
            spread = spread_bps_from_depth(t.symbol)
            sc, br = scalp_score(t, ins, spread)
            grid = GridSnapshot(
                ins.book_direction,
                ins.book_bid_share,
                0,
                0,
                0,
                0,
                False,
                [],
            )
            out.append(Candidate(t, ins, grid, sc, br))
        except (urllib.error.URLError, ValueError, OSError):
            continue
    out.sort(key=lambda c: c.local_score, reverse=True)
    return out[:max_analyze]


def scalp_candidate_payload(c: Candidate) -> dict[str, Any]:
    spread = float(c.score_breakdown.get("spread_bps", 0))
    return {
        "symbol": c.ticker.symbol,
        "scalp_score": round(c.local_score, 2),
        "score_breakdown": {k: round(v, 2) for k, v in c.score_breakdown.items() if k != "spread_bps"},
        "spread_bps": round(spread, 2),
        "last_price": c.ticker.last,
        "quote_volume_24h": round(c.ticker.quote_volume, 0),
        "range_24h_pct": round(c.ticker.range_pct, 2),
        "change_24h_pct": round(c.ticker.change_pct, 2),
        "book_bid_share_pct": round(c.insight.book_bid_share, 1),
        "trend": c.insight.trend,
    }


def collect_universe(
    base: str,
    *,
    min_volume: float,
    pool_size: int,
) -> list[TickerRow]:
    """Volatile movers first (one-off trades), not BTC/ETH volume leaders."""
    allowed = trading_usdt_perps(base)
    tickers = fetch_tickers(base, allowed)
    liquid = [t for t in tickers if t.quote_volume >= min_volume]

    volatile = sorted(liquid, key=lambda t: t.range_pct, reverse=True)[:pool_size]
    movers = sorted(liquid, key=lambda t: abs(t.change_pct), reverse=True)[:pool_size]
    gainers = sorted(liquid, key=lambda t: t.change_pct, reverse=True)[: pool_size // 2]
    losers = sorted(liquid, key=lambda t: t.change_pct)[: pool_size // 2]

    by_sym: dict[str, TickerRow] = {}
    for bucket in (volatile, movers, gainers, losers):
        for t in bucket:
            by_sym.setdefault(t.symbol, t)
    return list(by_sym.values())


def enrich_candidates(
    base: str,
    tickers: list[TickerRow],
    api: str,
    sec: str,
    *,
    max_analyze: int,
) -> list[Candidate]:
    """Quick local score on all, deep grid analysis on top N."""
    prelim: list[tuple[TickerRow, SymbolInsight, float, dict[str, float]]] = []
    for t in tickers:
        try:
            ins = build_insight(base, t.symbol, t)
        except (urllib.error.URLError, ValueError):
            continue
        dummy_grid = GridSnapshot("LONG", ins.book_bid_share, 4, 8, 0, 0, False, [])
        sc, br = local_score(t, ins, dummy_grid)
        prelim.append((t, ins, sc, br))

    prelim.sort(key=lambda x: quick_volatile_rank(x[0]), reverse=True)
    top = prelim[:max_analyze]

    out: list[Candidate] = []
    for t, ins, _, _ in top:
        grid = analyze_grid(t.symbol, api, sec)
        if grid is None:
            continue
        sc, br = local_score(t, ins, grid)
        out.append(Candidate(t, ins, grid, sc, br))
    out.sort(key=lambda c: c.local_score, reverse=True)
    return out


def candidate_payload(c: Candidate) -> dict[str, Any]:
    return {
        "symbol": c.ticker.symbol,
        "local_score": round(c.local_score, 2),
        "score_breakdown": {k: round(v, 2) for k, v in c.score_breakdown.items()},
        "last_price": c.ticker.last,
        "change_24h_pct": round(c.ticker.change_pct, 2),
        "range_24h_pct": round(c.ticker.range_pct, 2),
        "quote_volume_24h": round(c.ticker.quote_volume, 0),
        "trend": c.insight.trend,
        "trend_note": c.insight.trend_note,
        "momentum_1h_pct": round(c.insight.change_1h, 2),
        "momentum_4h_pct": round(c.insight.change_4h, 2),
        "book_auto_direction": c.grid.auto_direction,
        "book_bid_share_pct": round(c.grid.bid_share_pct, 1),
        "dca_walls_found": c.grid.dca_walls,
        "dca_walls_target": c.grid.dca_target,
        "grid_notional_usdt": round(c.grid.grid_notional_usdt, 2),
        "entry_size_usdt": round(c.grid.entry_usdt, 2),
        "blocked_by_account_imbalance": c.grid.blocked_by_imbalance,
        "sample_wall_prices": c.grid.wall_prices,
        "funding_pct_8h": round(c.insight.funding_pct, 4),
    }


def deepseek_pick(candidates: list[Candidate], api_key: str, *, model: str, base_url: str) -> dict[str, Any]:
    payload = [candidate_payload(c) for c in candidates if not c.grid.blocked_by_imbalance]
    if not payload:
        payload = [candidate_payload(c) for c in candidates[:1]]

    system = (
        "You are a crypto futures trading assistant for ONE manual volatile trade. "
        "The user already runs a separate supervisor fleet (FUTURES_PAIRS); "
        "this pick is a single extra trade on a more volatile alt. "
        "Prefer: high 24h range (10-30%), decent liquidity, clear OB walls, "
        "book AUTO direction aligned with short-term momentum. "
        "Avoid: sleepy majors (low range), extreme blow-offs (>35% 24h), blocked symbols. "
        "Respond with JSON only: "
        '{"symbol":"SYMBOL","direction":"long|short","confidence":0.0-1.0,"reason":"one short paragraph"}'
    )
    user = (
        "Candidates ranked by local score (liquidity + volatility + trend/book alignment + walls):\n"
        + json.dumps(payload, indent=2)
        + "\n\nPick ONE winner. direction must match book_auto_direction unless you have strong reason not to."
    )

    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def deepseek_pick_scalp(
    candidates: list[Candidate], api_key: str, *, model: str, base_url: str,
) -> dict[str, Any]:
    payload = [scalp_candidate_payload(c) for c in candidates[:8]]
    system = (
        "You pick ONE Binance USDT-M perpetual for a short-term order-book scalp bot. "
        "Prefer: high 24h quote volume, tight spread (low spread_bps), moderate 24h range (4-15%), "
        "not extreme blow-offs (>25% 24h move). Avoid illiquid micro-caps. "
        "Respond JSON only: "
        '{"symbol":"SYMBOL","confidence":0.0-1.0,"reason":"one short paragraph"}'
    )
    user = "Scalp candidates (liquidity + spread + stability):\n" + json.dumps(payload, indent=2)

    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return json.loads(data["choices"][0]["message"]["content"])


def print_ranking(candidates: list[Candidate], limit: int = 8) -> None:
    print(f"\n{DIM}Local score ranking (top {limit}):{RESET}")
    for i, c in enumerate(candidates[:limit], 1):
        flag = f"{RED}BLOCK{RESET}" if c.grid.blocked_by_imbalance else c.grid.auto_direction
        print(
            f"  {i:>2}. {c.ticker.symbol:<16} score {c.local_score:5.1f}  "
            f"range {c.ticker.range_pct:5.1f}%  24h {_fmt_pct_plain(c.ticker.change_pct)}  "
            f"{flag}  walls {c.grid.dca_walls}/{c.grid.dca_target}"
        )


def print_ranking_scalp(candidates: list[Candidate], limit: int = 8) -> None:
    print(f"\n{DIM}Scalp score ranking (top {limit}):{RESET}")
    for i, c in enumerate(candidates[:limit], 1):
        spread = c.score_breakdown.get("spread_bps", 0)
        vol_m = c.ticker.quote_volume / 1_000_000
        print(
            f"  {i:>2}. {c.ticker.symbol:<16} score {c.local_score:5.1f}  "
            f"vol {vol_m:5.0f}M  spread {spread:4.1f}bps  "
            f"range {c.ticker.range_pct:5.1f}%  24h {_fmt_pct_plain(c.ticker.change_pct)}"
        )


def _fmt_pct_plain(x: float) -> str:
    return f"{x:+.1f}%"


def print_pick(pick: dict[str, Any], *, source: str, dry_run: bool = False) -> None:
    sym = pick.get("symbol", "").upper()
    direction = str(pick.get("direction", "auto")).lower()
    conf = pick.get("confidence", "?")
    reason = pick.get("reason", "")
    print(f"\n{BOLD}{GREEN}▶ Trade pick ({source}){RESET}")
    print(f"  {BOLD}Symbol{RESET}     {CYAN}{sym}{RESET}")
    print(f"  {BOLD}Direction{RESET}  {direction.upper()}")
    print(f"  {BOLD}Confidence{RESET} {conf}")
    print(f"  {BOLD}Reason{RESET}     {reason}")
    if dry_run:
        print(f"\n  {DIM}Live run:{RESET}  dca {sym}" + (f" {direction}" if direction in ("long", "short") else ""))
    else:
        print(f"\n  {DIM}Preview grid:{RESET}  pick --dry-run")
        print(f"  {DIM}Run:{RESET}  {BOLD}pick -y{RESET}  (or  dca {sym}" + (f" {direction}" if direction in ("long", "short") else "") + ")")
        print(f"  {DIM}Background:{RESET}  pick -y --bg")


def print_pick_scalp(pick: dict[str, Any], *, source: str) -> None:
    sym = pick.get("symbol", "").upper()
    conf = pick.get("confidence", "?")
    reason = pick.get("reason", "")
    cmd = (
        f"obscalp {sym} --execute --bar-sec 60 --sample-sec 2 "
        f"--imb-long 0.58 --imb-short 0.42 "
        f"--tp-pct 0.35 --sl-pct 0.25 --fee-buffer 0.08 --momentum-min-pct 0.05"
    )
    print(f"\n{BOLD}{GREEN}▶ OB scalp pick ({source}){RESET}")
    print(f"  {BOLD}Symbol{RESET}     {CYAN}{sym}{RESET}")
    print(f"  {BOLD}Confidence{RESET} {conf}")
    print(f"  {BOLD}Reason{RESET}     {reason}")
    print(f"\n  {DIM}Observe:{RESET}  obscalp {sym} --dry-run --bar-sec 60")
    print(f"  {DIM}Run stack:{RESET}  {BOLD}obscalp-pick -y{RESET}  or  pick --scalp -y")


def run_grid_dry_run(pick: dict[str, Any]) -> int:
    """Show full OB grid preview for the picked symbol (no orders sent)."""
    sym = pick.get("symbol", "").upper()
    direction = str(pick.get("direction", "auto")).lower()
    if direction not in ("long", "short", "auto"):
        direction = "auto"

    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "orderbook_dca_grid.py")
    cmd = [
        sys.executable, script, sym,
        "--dry-run",
        "--direction", direction,
        "--recv-window", os.getenv("RECV_WINDOW", "15000"),
    ]
    print(f"\n{BOLD}{CYAN}Grid dry-run · {sym} {direction.upper()}{RESET}\n")
    return subprocess.call(cmd, cwd=root)


def run_execute(pick: dict[str, Any], *, background: bool) -> int:
    """Start DCA supervisor for the picked symbol (live orders)."""
    sym = pick.get("symbol", "").upper()
    direction = str(pick.get("direction", "auto")).lower()
    if direction not in ("long", "short", "auto"):
        direction = "auto"

    root = os.path.dirname(os.path.abspath(__file__))

    if background:
        import botctl

        msg = botctl.start(sym, direction=direction if direction != "auto" else None)
        print(f"\n{BOLD}{GREEN}{msg}{RESET}")
        return 1 if msg.startswith(("❌", "⛔")) else 0

    dca = os.path.join(root, "dca")
    cmd = [dca, sym]
    if direction in ("long", "short"):
        cmd.append(direction)
    print(f"\n{BOLD}{GREEN}▶ Executing:{RESET} {' '.join(cmd)}\n")
    try:
        return subprocess.call(cmd, cwd=root)
    except KeyboardInterrupt:
        print(f"\n{DIM}Supervisor stopped (Ctrl+C). Open orders left on Binance.{RESET}")
        return 130


def run_scalp_execute(pick: dict[str, Any]) -> int:
    from ob_scalp_stack import switch_stack

    sym = pick.get("symbol", "").upper()
    print(f"\n{BOLD}{GREEN}▶ Executing OB scalp stack on {sym}{RESET}\n")
    try:
        pids = switch_stack(
            sym,
            execute=True,
            meta={"pick_reason": pick.get("reason", ""), "source": "trade_pick"},
        )
        print(f"{GREEN}Started{RESET} autotune={pids.get('autotune_pid')} watch={pids.get('watch_pid')}")
        print(f"{DIM}tail -f .run/logs/{sym}/scalp_session.log{RESET}")
        return 0
    except KeyboardInterrupt:
        print(f"\n{DIM}OB scalp stopped (Ctrl+C).{RESET}")
        return 130


def fallback_pick(candidates: list[Candidate]) -> dict[str, Any]:
    for c in candidates:
        if not c.grid.blocked_by_imbalance and c.grid.dca_walls >= 3:
            return {
                "symbol": c.ticker.symbol,
                "direction": c.grid.auto_direction.lower(),
                "confidence": min(0.85, 0.45 + c.local_score / 100),
                "reason": (
                    f"Highest local score ({c.local_score:.1f}): "
                    f"{c.grid.dca_walls} OB walls, {c.insight.trend} trend, "
                    f"vol {_fmt_pct_plain(c.ticker.change_pct)} / range {c.ticker.range_pct:.1f}%."
                ),
            }
    c = candidates[0]
    return {
        "symbol": c.ticker.symbol,
        "direction": c.grid.auto_direction.lower(),
        "confidence": 0.3,
        "reason": "Fallback: best available local score (no DeepSeek or all blocked).",
    }


def fallback_pick_scalp(candidates: list[Candidate]) -> dict[str, Any]:
    c = candidates[0]
    spread = c.score_breakdown.get("spread_bps", 0)
    vol_m = c.ticker.quote_volume / 1_000_000
    return {
        "symbol": c.ticker.symbol,
        "confidence": min(0.9, 0.5 + c.local_score / 100),
        "reason": (
            f"Highest scalp score ({c.local_score:.1f}): "
            f"{vol_m:.0f}M USDT vol, spread {spread:.1f}bps, "
            f"range {c.ticker.range_pct:.1f}%, 24h {_fmt_pct_plain(c.ticker.change_pct)}."
        ),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pick one futures trade: volatile DCA (default) or liquid OB scalp (--scalp).",
    )
    p.add_argument(
        "--scalp", action="store_true",
        help="Pick for orderbook_ob_scalp (liquid + tight spread), not DCA grid",
    )
    p.add_argument("--min-volume", type=float, default=None, metavar="USDT",
                   help="Min 24h quote volume (default: 5M DCA / 50M scalp)")
    p.add_argument("--min-range", type=float, default=8.0, metavar="PCT",
                   help="Min 24h high-low range %% (default: 8 — filters sleepy pairs)")
    p.add_argument("--max-range", type=float, default=28.0, metavar="PCT",
                   help="Scalp only: skip if 24h range above this (default: 28)")
    p.add_argument("--pool", type=int, default=20, help="Pool size per volatile/movers list")
    p.add_argument("--analyze", type=int, default=10, help="Deep OB analysis on top N by local score")
    p.add_argument("--base", default=FAPI_BASE)
    p.add_argument("--local-only", action="store_true", help="Skip DeepSeek; use highest local score")
    p.add_argument("--dry-run", action="store_true",
                   help="After pick, preview the full grid (no orders sent)")
    p.add_argument("-y", "--execute", action="store_true",
                   help="After pick, start dca supervisor immediately (live orders)")
    p.add_argument("--bg", action="store_true",
                   help="With --execute, start in background via botctl (returns to shell)")
    p.add_argument("--include-fleet", action="store_true",
                   help="Do not skip FUTURES_PAIRS from .env (default: skip fleet)")
    p.add_argument("--json", action="store_true", help="Print pick as JSON")
    p.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL))
    p.add_argument("--deepseek-base", default=os.getenv("DEEPSEEK_BASE", DEEPSEEK_DEFAULT_BASE))
    return p.parse_args()


def main() -> None:
    from orderbook_dca_grid import load_env_file, load_keys

    args = parse_args()
    load_env_file(None)
    base = args.base.rstrip("/")

    if args.min_volume is None:
        args.min_volume = 50_000_000 if args.scalp else 5_000_000
    if args.scalp and args.min_range == 8.0:
        args.min_range = 2.0

    api, sec = load_keys(None)
    if not api or not sec:
        print(f"{RED}BINANCE_API_KEY / BINANCE_SECRET_KEY required in .env.{RESET}", file=sys.stderr)
        sys.exit(1)

    if args.scalp:
        print(f"{BOLD}OB scalp pick · liquid pairs (volume + spread){RESET}")
        skip = set()
    else:
        print(f"{BOLD}Trade pick · volatile one-off (skips FUTURES_PAIRS fleet){RESET}")
        skip = fleet_pairs_to_skip(include_fleet=args.include_fleet)
        if skip and not args.json:
            print(f"{DIM}Skipping fleet (FUTURES_PAIRS): {', '.join(sorted(skip))}{RESET}")

    try:
        if args.scalp:
            universe = collect_universe_scalp(
                base, min_volume=args.min_volume, pool_size=args.pool,
            )
            universe = apply_skip(universe, skip)
            universe = [
                t for t in universe
                if args.min_range <= t.range_pct <= args.max_range
            ]
            if not universe:
                print(
                    f"{RED}No scalp candidates (vol≥{args.min_volume/1e6:.0f}M, "
                    f"range {args.min_range:g}-{args.max_range:g}%).{RESET}",
                    file=sys.stderr,
                )
                sys.exit(1)
            candidates = enrich_scalp_candidates(base, universe, max_analyze=args.analyze)
        else:
            universe = collect_universe(base, min_volume=args.min_volume, pool_size=args.pool)
            universe = apply_skip(universe, skip)
            universe = [t for t in universe if t.range_pct >= args.min_range]
            if not universe:
                print(f"{RED}No volatile candidates left (min range {args.min_range:g}%).{RESET}", file=sys.stderr)
                sys.exit(1)
            candidates = enrich_candidates(base, universe, api, sec, max_analyze=args.analyze)
    except urllib.error.URLError as exc:
        print(f"{RED}API error: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    if not candidates:
        print(f"{RED}No candidates after analysis.{RESET}", file=sys.stderr)
        sys.exit(1)

    if args.scalp:
        print_ranking_scalp(candidates)
    else:
        print_ranking(candidates)

    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    source = "local score"
    pick: dict[str, Any]

    if args.scalp:
        if args.local_only or not deepseek_key:
            if not deepseek_key and not args.local_only:
                print(f"\n{YELLOW}DEEPSEEK_API_KEY not set — using local scalp score.{RESET}")
            pick = fallback_pick_scalp(candidates)
        else:
            try:
                pick = deepseek_pick_scalp(
                    candidates, deepseek_key, model=args.model, base_url=args.deepseek_base,
                )
                source = "DeepSeek"
            except (urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
                print(f"\n{YELLOW}DeepSeek failed ({exc}) — fallback to local score.{RESET}")
                pick = fallback_pick_scalp(candidates)
    elif args.local_only or not deepseek_key:
        if not deepseek_key and not args.local_only:
            print(f"\n{YELLOW}DEEPSEEK_API_KEY not set — using local score only.{RESET}")
        pick = fallback_pick(candidates)
    else:
        try:
            pick = deepseek_pick(candidates, deepseek_key, model=args.model, base_url=args.deepseek_base)
            source = "DeepSeek"
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"\n{YELLOW}DeepSeek failed ({exc}) — fallback to local score.{RESET}")
            pick = fallback_pick(candidates)

    if args.json:
        payload_key = "scalp_candidates" if args.scalp else "candidates"
        cand_fn = scalp_candidate_payload if args.scalp else candidate_payload
        print(json.dumps({
            "mode": "scalp" if args.scalp else "dca",
            "pick": pick,
            "source": source,
            payload_key: [cand_fn(c) for c in candidates[:8]],
        }, indent=2))
    elif args.scalp:
        print_pick_scalp(pick, source=source)
    else:
        print_pick(pick, source=source, dry_run=args.dry_run)

    if args.scalp and args.execute and not args.json:
        sys.exit(run_scalp_execute(pick))
    elif not args.scalp and args.dry_run and not args.json:
        run_grid_dry_run(pick)
    elif not args.scalp and args.execute and not args.dry_run and not args.json:
        sys.exit(run_execute(pick, background=args.bg))


if __name__ == "__main__":
    main()
