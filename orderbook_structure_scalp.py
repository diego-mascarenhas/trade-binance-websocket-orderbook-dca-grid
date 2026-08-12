#!/usr/bin/env python3
"""Structure scalper: follow EMA bias, scale on book holds, partial TP on rejects.

Separate from orderbook_ob_scalp.py (multi-trigger/ML) and from capital-reduce
(underwater recovery). This bot trades *with* the bias:

  Bias bullish → LONG only
    · Support touch/hold  → ENTRY or ADD (compensate)
    · Resistance reject   → PARTIAL TP
    · Bias flip bearish   → CLOSE ALL

  Bias bearish → SHORT only (mirror)
  Bias flat    → no new entries; optional flatten

Uses Futures bookTicker WebSocket for live marks; REST for EMA, depth walls,
and orders.

Examples:
  ./obstructure-scalp XRPUSDT                 # dry-run (default)
  ./obstructure-scalp XRPUSDT --execute
  ./obstructure-scalp XRPUSDT --execute --base-size 25 --min-tp-pct 0.30
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import orderbook_dca_grid as grid
from ob_ema import fetch_ema_snapshot
from orderbook_dca_grid import (
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    BOLD,
    CYAN,
    _dec_places,
    _round_to,
    _signed_request,
    fetch_depth,
    get_wallet_balance,
    load_env_file,
    load_keys,
    load_symbol_filters,
    market_close_position,
    price_fmt,
    select_walls,
)

TAG = "obss"  # orderbook structure scalp
FSTREAM_BASE = os.getenv("BINANCE_FSTREAM", "wss://fstream.binance.com")
ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


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


def client_id(symbol: str, tag: str) -> str:
    tok = int(time.time() * 1000) % 1_000_000
    return f"{TAG}{tag}{symbol.upper()}{tok:06d}"[:36]


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


def market_open(
    symbol: str,
    is_long: bool,
    qty_str: str,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    cid: str,
) -> dict[str, Any]:
    side = "BUY" if is_long else "SELL"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": cid,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    return _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def market_reduce(
    symbol: str,
    is_long: bool,
    qty: float,
    hedge: bool,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
    recv: int,
) -> float:
    """Close `qty` of the position (reduce-only)."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    qd = _round_to(qty, step, ROUND_DOWN)
    if qd < filt["min_qty"]:
        return 0.0
    qty_str = f"{qd:.{qty_dp}f}"
    side = "SELL" if is_long else "BUY"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": client_id(symbol, "TP"),
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)
    return float(qd)


@dataclass
class ScalpState:
    phase: str = "FLAT"  # FLAT | LONG | SHORT
    qty: float = 0.0
    entry: float = 0.0
    adds: int = 0
    partials: int = 0
    realized: float = 0.0
    last_entry_ts: float = 0.0
    last_tp_ts: float = 0.0
    last_bias: str = "flat"
    notes: list[str] = field(default_factory=list)


def state_path(symbol: str) -> Path:
    d = LOG_ROOT / symbol.upper()
    d.mkdir(parents=True, exist_ok=True)
    return d / "structure_scalp.json"


def load_state(symbol: str) -> ScalpState:
    path = state_path(symbol)
    if not path.exists():
        return ScalpState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ScalpState(
            phase=str(raw.get("phase") or "FLAT"),
            qty=float(raw.get("qty") or 0),
            entry=float(raw.get("entry") or 0),
            adds=int(raw.get("adds") or 0),
            partials=int(raw.get("partials") or 0),
            realized=float(raw.get("realized") or 0),
            last_entry_ts=float(raw.get("last_entry_ts") or 0),
            last_tp_ts=float(raw.get("last_tp_ts") or 0),
            last_bias=str(raw.get("last_bias") or "flat"),
        )
    except Exception:
        return ScalpState()


def save_state(symbol: str, st: ScalpState) -> None:
    path = state_path(symbol)
    path.write_text(
        json.dumps(
            {
                "phase": st.phase,
                "qty": st.qty,
                "entry": st.entry,
                "adds": st.adds,
                "partials": st.partials,
                "realized": st.realized,
                "last_entry_ts": st.last_entry_ts,
                "last_tp_ts": st.last_tp_ts,
                "last_bias": st.last_bias,
                "ts": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def fetch_near_walls(
    symbol: str,
    mark: float,
    *,
    count: int,
    min_gap: float,
    min_dist: float,
    max_range: float,
    limit: int,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    depth = fetch_depth(symbol, limit)
    bids = [[float(p), float(q)] for p, q in (depth.get("bids") or [])]
    asks = [[float(p), float(q)] for p, q in (depth.get("asks") or [])]
    supports = select_walls(bids, mark, True, count, min_gap, min_dist, max_range)
    resistances = select_walls(asks, mark, False, count, min_gap, min_dist, max_range)
    return supports, resistances


def wall_hold(
    mark: float,
    wall: float,
    *,
    is_support: bool,
    touch_pct: float,
    break_pct: float,
) -> str:
    """hold | far | broken"""
    if mark <= 0 or wall <= 0:
        return "far"
    if is_support:
        if mark < wall * (1.0 - break_pct / 100.0):
            return "broken"
        if mark <= wall * (1.0 + touch_pct / 100.0):
            return "hold"
        return "far"
    if mark > wall * (1.0 + break_pct / 100.0):
        return "broken"
    if mark >= wall * (1.0 - touch_pct / 100.0):
        return "hold"
    return "far"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    load_env_file(None)
    p = argparse.ArgumentParser(
        description="Structure scalper: EMA bias + support/resistance holds (WS)",
    )
    p.add_argument("symbol", help="e.g. XRPUSDT")
    p.add_argument("--execute", action="store_true", help="Live orders (default is dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    p.add_argument("--base-size", type=float, default=_env_float("OBSS_BASE_SIZE", 20.0),
                   help="Entry notional USDT (default 20)")
    p.add_argument("--add-mult", type=float, default=_env_float("OBSS_ADD_MULT", 1.0),
                   help="Add size = base-size × this (default 1)")
    p.add_argument("--max-adds", type=int, default=_env_int("OBSS_MAX_ADDS", 2),
                   help="Max adds after entry (default 2)")
    p.add_argument("--partial-pct", type=float, default=_env_float("OBSS_PARTIAL_PCT", 50.0),
                   help="%% of position to TP on resistance/support reject (default 50)")
    p.add_argument(
        "--min-tp-pct",
        type=float,
        default=_env_float("OBSS_MIN_TP_PCT", 0.25),
        help="Min favorable move %% vs entry before PARTIAL TP (default 0.25). "
             "Plus --fee-buffer. Env: OBSS_MIN_TP_PCT",
    )
    p.add_argument(
        "--fee-buffer",
        type=float,
        default=_env_float("OBSS_FEE_BUFFER", 0.08),
        help="Round-trip fee allowance %% added to min-tp (default 0.08). Env: OBSS_FEE_BUFFER",
    )
    p.add_argument(
        "--max-partials",
        type=int,
        default=_env_int("OBSS_MAX_PARTIALS", 2),
        help="Max partial TPs per position before requiring CLOSE ALL (default 2)",
    )
    p.add_argument("--bias-interval", default=os.getenv("OBSS_BIAS_INTERVAL", "15m") or "15m",
                   help="EMA timeframe for bias (default 15m)")
    p.add_argument("--ema-fast", type=int, default=_env_int("OBSS_EMA_FAST", 7))
    p.add_argument("--ema-slow", type=int, default=_env_int("OBSS_EMA_SLOW", 25))
    p.add_argument("--slope-min", type=float, default=_env_float("OBSS_SLOPE_MIN", 0.05))
    p.add_argument("--flatten-flat", action="store_true",
                   help="Close position when bias turns flat")
    p.add_argument(
        "--adopt",
        action="store_true",
        help="Manage an existing exchange position (default: ignore orphans; only trade what we open)",
    )
    p.add_argument("--touch-pct", type=float, default=_env_float("OBSS_TOUCH_PCT", 0.35))
    p.add_argument("--break-pct", type=float, default=_env_float("OBSS_BREAK_PCT", 0.15))
    p.add_argument("--struct-count", type=int, default=_env_int("OBSS_STRUCT_COUNT", 2))
    p.add_argument("--min-gap", type=float, default=_env_float("OBSS_MIN_GAP", 0.4))
    p.add_argument("--min-dist", type=float, default=_env_float("OBSS_MIN_DIST", 0.05))
    p.add_argument("--max-range", type=float, default=_env_float("OBSS_MAX_RANGE", 3.0))
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--entry-cooldown", type=float, default=_env_float("OBSS_ENTRY_CD", 45.0))
    p.add_argument("--tp-cooldown", type=float, default=_env_float("OBSS_TP_CD", 90.0),
                   help="Seconds between partial TPs (default 90)")
    p.add_argument("--bias-refresh", type=float, default=_env_float("OBSS_BIAS_REFRESH", 60.0),
                   help="Seconds between EMA bias REST refresh (default 60)")
    p.add_argument("--wall-refresh", type=float, default=_env_float("OBSS_WALL_REFRESH", 20.0),
                   help="Seconds between depth wall refresh (default 20)")
    p.add_argument("--recv-window", type=int, default=_env_int("RECV_WINDOW", 15000))
    p.add_argument("--env-file", default=None)
    args = p.parse_args(argv)
    if args.execute:
        args.dry_run = False
    else:
        args.dry_run = True
    args.symbol = args.symbol.upper()
    args.partial_pct = max(5.0, min(100.0, float(args.partial_pct)))
    args.base_size = max(5.0, float(args.base_size))
    args.max_adds = max(0, min(10, int(args.max_adds)))
    args.min_tp_pct = max(0.05, min(5.0, float(args.min_tp_pct)))
    args.fee_buffer = max(0.0, min(1.0, float(args.fee_buffer)))
    args.max_partials = max(1, min(10, int(args.max_partials)))
    args.tp_cooldown = max(15.0, float(args.tp_cooldown))
    return args


def refresh_bias(args: argparse.Namespace) -> tuple[str, str]:
    snap = fetch_ema_snapshot(
        args.symbol,
        interval=str(args.bias_interval),
        fast=int(args.ema_fast),
        slow=int(args.ema_slow),
        slope_min_pct=float(args.slope_min),
    )
    if snap is None:
        return "flat", "EMA unavailable"
    label = (
        f"EMA{args.ema_fast}/{args.ema_slow} {args.bias_interval} "
        f"trend={snap.trend} slope={snap.slope_pct:+.3f}%"
    )
    return snap.trend, label


def run(args: argparse.Namespace) -> int:
    try:
        import websocket
    except ImportError:
        print(f"{RED}Install websocket-client: .venv/bin/pip install websocket-client{RESET}")
        return 2

    sym = args.symbol
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}Missing API keys{RESET}")
        return 2
    recv = args.recv_window
    filt = load_symbol_filters(sym)
    hedge = False
    try:
        resp = _signed_request("GET", "/fapi/v1/positionSide/dual", {}, api, sec, recv)
        hedge = bool(resp.get("dualSidePosition"))
    except Exception:
        pass

    st = load_state(sym)
    bias, bias_label = refresh_bias(args)
    st.last_bias = bias
    save_state(sym, st)

    print(f"{BOLD}{CYAN}structure-scalp{RESET}  {sym}  ·  "
          f"{'DRY-RUN' if args.dry_run else f'{RED}LIVE{RESET}'}")
    print(f"{DIM}Bias · {bias_label}{RESET}")
    need = float(args.min_tp_pct) + float(args.fee_buffer)
    print(
        f"{DIM}Entry ${args.base_size:g}  ·  adds≤{args.max_adds} ×{args.add_mult:g}  ·  "
        f"partial {args.partial_pct:g}% (max {args.max_partials}, cd {args.tp_cooldown:g}s)  ·  "
        f"TP only if edge≥{need:.2f}%  ·  Ctrl+C stop{RESET}"
    )

    live: dict[str, Any] = {
        "mark": 0.0,
        "hi": 0.0,
        "lo": 0.0,
        "supports": [],
        "resistances": [],
        "bias": bias,
        "bias_label": bias_label,
        "last_bias_ts": time.time(),
        "last_wall_ts": 0.0,
        "ticks": 0,
        "lock": threading.Lock(),
        "busy": False,
        "stop": False,
        "status": "boot",
        "event": "",
        "orphan": False,
    }
    paint_lock = threading.Lock()

    def _paint() -> None:
        mark = float(live["mark"] or 0)
        bias_now = str(live["bias"])
        bc = GREEN if bias_now == "bullish" else (RED if bias_now == "bearish" else DIM)
        ts = time.strftime("%H:%M:%S")
        phase = st.phase
        pc = GREEN if phase == "LONG" else (RED if phase == "SHORT" else DIM)
        line = (
            f"\r{DIM}{ts}{RESET} {BOLD}{sym}{RESET}  "
            f"mark {price_fmt(mark) if mark else '—':>10}  "
            f"bias {bc}{bias_now:<8}{RESET}  "
            f"pos {pc}{phase:<5}{RESET} q={grid.qty_fmt(st.qty) if st.qty else '—'}  "
            f"adds {st.adds}/{args.max_adds}  tp#{st.partials}  "
            f"[{live['status']}] {DIM}{live['event']}{RESET}   "
        )
        with paint_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _break() -> None:
        with paint_lock:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _refresh_walls(mark: float) -> None:
        if mark <= 0:
            return
        try:
            sup, res = fetch_near_walls(
                sym, mark,
                count=args.struct_count,
                min_gap=args.min_gap,
                min_dist=args.min_dist,
                max_range=args.max_range,
                limit=args.limit,
            )
            live["supports"] = sup
            live["resistances"] = res
            live["last_wall_ts"] = time.time()
        except Exception as exc:
            live["event"] = f"walls-err {exc}"

    def _sync_pos_from_exchange() -> None:
        side_is_long, qty, entry = grid._detect_open_side(sym, hedge, api, sec, recv)
        if side_is_long is None or qty <= 0:
            if st.phase != "FLAT":
                st.phase = "FLAT"
                st.qty = 0.0
                st.entry = 0.0
                save_state(sym, st)
            return
        st.phase = "LONG" if side_is_long else "SHORT"
        st.qty = float(qty)
        st.entry = float(entry)
        save_state(sym, st)

    def _do_entry(is_long: bool, reason: str) -> None:
        if live.get("orphan"):
            live["event"] = "blocked: orphan pos (use --adopt)"
            return
        mark = float(live["mark"] or 0)
        if mark <= 0:
            return
        now = time.time()
        if now - st.last_entry_ts < args.entry_cooldown:
            return
        notional = args.base_size
        qty_str, qty_f = qty_for_notional(notional, mark, filt)
        side = "LONG" if is_long else "SHORT"
        _break()
        print(f"{BOLD}{GREEN}▶ ENTRY {side}{RESET}  {reason}  ·  "
              f"{qty_str} @ ~{price_fmt(mark)} (~{notional:.2f} USDT)")
        if args.dry_run:
            print(f"{DIM}  dry-run: would MARKET {'BUY' if is_long else 'SELL'}{RESET}")
            st.phase = "LONG" if is_long else "SHORT"
            st.qty = qty_f
            st.entry = mark
            st.adds = 0
            st.partials = 0
            st.last_entry_ts = now
            save_state(sym, st)
            return
        cid = client_id(sym, "EN")
        market_open(sym, is_long, qty_str, hedge, api, sec, recv, cid=cid)
        st.last_entry_ts = now
        time.sleep(0.4)
        _sync_pos_from_exchange()
        st.adds = 0
        st.partials = 0
        save_state(sym, st)
        print(f"{GREEN}✓ opened{RESET}  qty={grid.qty_fmt(st.qty)} entry={price_fmt(st.entry)}")

    def _do_add(is_long: bool, reason: str) -> None:
        if st.adds >= args.max_adds:
            return
        mark = float(live["mark"] or 0)
        if mark <= 0 or st.qty <= 0:
            return
        now = time.time()
        if now - st.last_entry_ts < args.entry_cooldown:
            return
        notional = args.base_size * args.add_mult
        qty_str, qty_f = qty_for_notional(notional, mark, filt)
        _break()
        print(f"{BOLD}{CYAN}▶ ADD {st.phase}{RESET}  {reason}  ·  "
              f"{qty_str} @ ~{price_fmt(mark)} (add {st.adds + 1}/{args.max_adds})")
        if args.dry_run:
            # Simulate avg
            new_q = st.qty + qty_f
            st.entry = (st.entry * st.qty + mark * qty_f) / new_q if new_q else mark
            st.qty = new_q
            st.adds += 1
            st.last_entry_ts = now
            save_state(sym, st)
            print(f"{DIM}  dry-run: qty→{grid.qty_fmt(st.qty)} avg→{price_fmt(st.entry)}{RESET}")
            return
        cid = client_id(sym, "AD")
        market_open(sym, is_long, qty_str, hedge, api, sec, recv, cid=cid)
        st.adds += 1
        st.last_entry_ts = now
        time.sleep(0.4)
        _sync_pos_from_exchange()
        save_state(sym, st)

    def _tp_edge_ok(is_long: bool, mark: float) -> tuple[bool, float]:
        """Require mark favorably past entry by min_tp + fee_buffer (%%)."""
        if st.entry <= 0 or mark <= 0:
            return False, 0.0
        move_pct = ((mark - st.entry) / st.entry * 100.0) if is_long else (
            (st.entry - mark) / st.entry * 100.0
        )
        need = float(args.min_tp_pct) + float(args.fee_buffer)
        return move_pct >= need, move_pct

    def _do_partial(is_long: bool, reason: str) -> None:
        if st.qty <= 0:
            return
        now = time.time()
        if now - st.last_tp_ts < args.tp_cooldown:
            return
        if st.partials >= int(args.max_partials):
            live["event"] = f"max partials ({st.partials})"
            return
        mark = float(live["mark"] or 0)
        ok, move_pct = _tp_edge_ok(is_long, mark)
        if not ok:
            need = float(args.min_tp_pct) + float(args.fee_buffer)
            live["status"] = "tp-wait"
            live["event"] = f"edge {move_pct:+.3f}% < need {need:.2f}% · {reason}"
            return
        min_n = float(filt["min_notional"])
        # If remaining after partial would be dust, close all instead (avoid shredding fees)
        close_qty = st.qty * (args.partial_pct / 100.0)
        remain = st.qty - close_qty
        if remain * mark < min_n * 1.5 or close_qty * mark < min_n:
            _do_close_all(f"full TP (dust avoid) · {reason} · edge {move_pct:+.3f}%")
            return
        _break()
        print(
            f"{BOLD}{YELLOW}▶ PARTIAL TP{RESET}  {reason}  ·  edge {move_pct:+.3f}%  ·  "
            f"close ~{args.partial_pct:g}% ({grid.qty_fmt(close_qty)}) @ ~{price_fmt(mark)}"
        )
        if args.dry_run:
            pnl = (mark - st.entry) * close_qty if is_long else (st.entry - mark) * close_qty
            st.qty = max(0.0, st.qty - close_qty)
            st.realized += pnl
            st.partials += 1
            st.last_tp_ts = now
            if st.qty * mark < min_n:
                st.phase = "FLAT"
                st.qty = 0.0
            save_state(sym, st)
            print(f"{DIM}  dry-run: rPnL {pnl:+.4f}  qty→{grid.qty_fmt(st.qty)}{RESET}")
            return
        closed = market_reduce(sym, is_long, close_qty, hedge, filt, api, sec, recv)
        # Approximate realized from mark vs entry (exchange fill may differ slightly)
        pnl = (mark - st.entry) * closed if is_long else (st.entry - mark) * closed
        st.realized += pnl
        st.partials += 1
        st.last_tp_ts = now
        time.sleep(0.4)
        _sync_pos_from_exchange()
        save_state(sym, st)
        print(f"{GREEN}✓ partial closed{RESET} {grid.qty_fmt(closed)}  rPnL~{pnl:+.4f}")

    def _do_close_all(reason: str) -> None:
        if st.phase == "FLAT" or st.qty <= 0:
            return
        is_long = st.phase == "LONG"
        mark = float(live["mark"] or 0)
        _break()
        print(f"{BOLD}{RED}▶ CLOSE ALL{RESET}  {reason}  ·  "
              f"qty={grid.qty_fmt(st.qty)} @ ~{price_fmt(mark)}")
        if args.dry_run:
            pnl = (mark - st.entry) * st.qty if is_long else (st.entry - mark) * st.qty
            st.realized += pnl
            st.phase = "FLAT"
            st.qty = 0.0
            st.adds = 0
            save_state(sym, st)
            print(f"{DIM}  dry-run: rPnL {pnl:+.4f}  flat{RESET}")
            return
        market_close_position(sym, is_long, float(st.qty), hedge, filt, api, sec, recv)
        time.sleep(0.4)
        st.phase = "FLAT"
        st.qty = 0.0
        st.adds = 0
        st.entry = 0.0
        save_state(sym, st)
        _sync_pos_from_exchange()
        print(f"{GREEN}✓ flat{RESET}")

    def _evaluate() -> None:
        """Core decision: bias + nearest wall hold/reject."""
        if live["busy"] or live["stop"]:
            return
        mark = float(live["mark"] or 0)
        if mark <= 0:
            return
        now = time.time()
        if now - float(live["last_bias_ts"]) >= args.bias_refresh:
            try:
                b, lab = refresh_bias(args)
                live["bias"] = b
                live["bias_label"] = lab
                live["last_bias_ts"] = now
                st.last_bias = b
                save_state(sym, st)
            except Exception as exc:
                live["event"] = f"bias-err {exc}"

        if now - float(live["last_wall_ts"]) >= args.wall_refresh:
            _refresh_walls(mark)

        bias_now = str(live["bias"])
        supports: list = live["supports"] or []
        resistances: list = live["resistances"] or []
        near_sup = supports[0][0] if supports else 0.0
        near_res = resistances[0][0] if resistances else 0.0
        sup_st = wall_hold(mark, near_sup, is_support=True,
                           touch_pct=args.touch_pct, break_pct=args.break_pct) if near_sup else "far"
        res_st = wall_hold(mark, near_res, is_support=False,
                           touch_pct=args.touch_pct, break_pct=args.break_pct) if near_res else "far"

        live["busy"] = True
        try:
            # Dust bag → flatten (avoid endless fee shredding)
            if st.phase in ("LONG", "SHORT") and st.qty > 0:
                min_n = float(filt["min_notional"])
                if st.qty * mark < min_n * 1.5:
                    _do_close_all(f"dust flatten (notional {st.qty * mark:.2f} < {min_n * 1.5:.2f})")
                    return

            # Bias flip → close
            if st.phase == "LONG" and bias_now == "bearish":
                _do_close_all(f"bias flip → {bias_now}")
                return
            if st.phase == "SHORT" and bias_now == "bullish":
                _do_close_all(f"bias flip → {bias_now}")
                return
            if args.flatten_flat and st.phase != "FLAT" and bias_now == "flat":
                _do_close_all("bias flat")
                return

            # Break against position → close
            if st.phase == "LONG" and near_sup and sup_st == "broken":
                _do_close_all(f"support broken @ {price_fmt(near_sup)}")
                return
            if st.phase == "SHORT" and near_res and res_st == "broken":
                _do_close_all(f"resistance broken @ {price_fmt(near_res)}")
                return

            # Partial TP only when in fee-covered profit + favorable wall hold
            if st.phase == "LONG" and res_st == "hold":
                live["status"] = "tp?"
                live["event"] = f"res hold {price_fmt(near_res)}"
                _do_partial(True, f"resistance hold {price_fmt(near_res)}")
                return
            if st.phase == "SHORT" and sup_st == "hold":
                live["status"] = "tp?"
                live["event"] = f"sup hold {price_fmt(near_sup)}"
                _do_partial(False, f"support hold {price_fmt(near_sup)}")
                return

            # Entry / add with bias
            if bias_now == "bullish":
                if st.phase == "FLAT" and sup_st == "hold":
                    live["status"] = "entry"
                    _do_entry(True, f"support hold {price_fmt(near_sup)}")
                    return
                if st.phase == "LONG" and sup_st == "hold" and st.adds < args.max_adds:
                    live["status"] = "add"
                    _do_add(True, f"support hold {price_fmt(near_sup)}")
                    return
            elif bias_now == "bearish":
                if st.phase == "FLAT" and res_st == "hold":
                    live["status"] = "entry"
                    _do_entry(False, f"resistance hold {price_fmt(near_res)}")
                    return
                if st.phase == "SHORT" and res_st == "hold" and st.adds < args.max_adds:
                    live["status"] = "add"
                    _do_add(False, f"resistance hold {price_fmt(near_res)}")
                    return

            live["status"] = "listening"
            live["event"] = (
                f"sup={sup_st}:{price_fmt(near_sup) if near_sup else '—'} "
                f"res={res_st}:{price_fmt(near_res) if near_res else '—'}"
            )
        finally:
            live["busy"] = False

    # Seed walls; optionally adopt exchange position
    side_is_long, qty, entry = grid._detect_open_side(sym, hedge, api, sec, recv)
    if side_is_long is not None and qty > 0:
        if args.adopt:
            _sync_pos_from_exchange()
            print(
                f"{YELLOW}Adopted exchange {st.phase} qty={grid.qty_fmt(st.qty)} "
                f"entry={price_fmt(st.entry)}{RESET}"
            )
        else:
            print(
                f"{YELLOW}Exchange has a {('LONG' if side_is_long else 'SHORT')} "
                f"qty={grid.qty_fmt(qty)} — ignored (pass --adopt to manage it).{RESET}"
            )
            live["orphan"] = True
            # Clear stale session state so we don't think we own the bag
            st.phase = "FLAT"
            st.qty = 0.0
            st.entry = 0.0
            st.adds = 0
            save_state(sym, st)
    elif st.phase in ("LONG", "SHORT"):
        # State says in trade but exchange flat → reset
        st.phase = "FLAT"
        st.qty = 0.0
        st.entry = 0.0
        st.adds = 0
        save_state(sym, st)
    try:
        mid = float(grid._live_mid(sym) or 0)
        if mid > 0:
            live["mark"] = mid
            _refresh_walls(mid)
    except Exception:
        pass

    url = (
        f"{FSTREAM_BASE.rstrip('/')}/stream?streams="
        f"{sym.lower()}@bookTicker/{sym.lower()}@kline_1m"
    )
    print(f"{DIM}{url}{RESET}")
    print()

    def on_message(_ws: Any, message: str) -> None:
        if live["stop"]:
            return
        try:
            msg = json.loads(message)
            data = msg.get("data") or msg
            stream = str(msg.get("stream") or "")
            ev = str(data.get("e") or "")
            if "bookTicker" in stream or ev == "bookTicker":
                try:
                    bid = float(data.get("b") or 0)
                    ask = float(data.get("a") or 0)
                except (TypeError, ValueError):
                    return
                if bid <= 0 or ask <= 0:
                    return
                mark = (bid + ask) / 2.0
                live["mark"] = mark
                live["ticks"] = int(live["ticks"]) + 1
                live["hi"] = max(float(live["hi"] or 0), mark) or mark
                live["lo"] = min(float(live["lo"] or mark), mark)
                _paint()
                # Throttle decisions (~4 Hz)
                if int(live["ticks"]) % 8 == 0:
                    _evaluate()
        except Exception as exc:
            live["event"] = f"err {exc}"

    def on_error(_ws: Any, error: Any) -> None:
        _break()
        print(f"{RED}WS error: {error}{RESET}")

    def on_close(_ws: Any, status: Any, msg: Any) -> None:
        _break()
        print(f"{YELLOW}WS closed ({status} {msg}) — reconnecting…{RESET}")

    def on_open(_ws: Any) -> None:
        _break()
        print(f"{GREEN}✓ WS connected{RESET} — structure scalp listening")
        live["status"] = "listening"
        _paint()

    def _run_ws() -> None:
        while not live["stop"]:
            app = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            live["ws"] = app
            try:
                app.run_forever(ping_interval=0, sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception as exc:
                _break()
                print(f"{RED}WS run error: {exc}{RESET}")
            if live["stop"]:
                break
            time.sleep(3.0)

    thread = threading.Thread(target=_run_ws, name="obss-ws", daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            _paint()
            time.sleep(0.5)
    except KeyboardInterrupt:
        live["stop"] = True
        try:
            app = live.get("ws")
            if app is not None:
                app.close()
        except Exception:
            pass
        _break()
        print(f"\n{YELLOW}structure-scalp stopped.{RESET}  "
              f"realized~{st.realized:+.4f}  phase={st.phase}")
        save_state(sym, st)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
