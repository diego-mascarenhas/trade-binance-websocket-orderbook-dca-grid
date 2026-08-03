"""Exit: soft-close on adverse pullback from the favorable extreme.

Tracks the best mark while the position is open (lowest for SHORT, highest
for LONG). When price retraces ``--pullback-pct`` from that extreme and the
trade is already green enough (``--pullback-min-profit-pct`` + fee buffer),
market-close like structure / OB-flip.

Independent primary exit via ``--exit pullback``. Optional ``--protect-be``
still works as a floor.
"""

from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal

from ob_signals import estimated_net_pct, profit_pct

_CLOSE_REASONS: dict[str, str] = {}


def pop_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.pop(symbol.upper(), None)


def peek_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.get(symbol.upper())


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def pullback_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "pullback_pct", None)
    if v is not None:
        return float(v)
    return _env_float("PULLBACK_PCT", 0.4)


def min_profit_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "pullback_min_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("PULLBACK_MIN_PROFIT_PCT", 0.3)


def _fee_buffer_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "tp_fee_buffer", None)
    if v is not None:
        return float(v)
    return _env_float("TP_FEE_BUFFER", 0.12)


def _update_extreme(is_long: bool, mark: float, prev: float | None) -> float:
    if prev is None or prev <= 0:
        return mark
    if is_long:
        return max(prev, mark)
    return min(prev, mark)


def _giveback_pct(is_long: bool, extreme: float, mark: float) -> float:
    """Adverse move from favorable extreme, in %% of extreme."""
    if extreme <= 0 or mark <= 0:
        return 0.0
    if is_long:
        # Pullback = drop from high
        return max(0.0, (extreme - mark) / extreme * 100)
    # Pullback = bounce from low
    return max(0.0, (mark - extreme) / extreme * 100)


def run_once(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged
    from exits.structure import cancel_close_algos

    if entry <= 0 or qty <= 0:
        return

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    side = "LONG" if side_is_long else "SHORT"
    pb = pullback_pct(args)
    min_pct = min_profit_pct(args)
    fee_buf = _fee_buffer_pct(args)

    try:
        mark = staged.get_mark_price(symbol, api, sec, recv)
    except Exception as exc:
        print(f"{grid.YELLOW}Pullback mark skip: {exc}{grid.RESET}")
        return
    if mark <= 0:
        return

    state = staged.load_state(symbol.upper())
    prev_ext = state.get("pullback_extreme")
    try:
        prev_f = float(prev_ext) if prev_ext is not None else None
    except (TypeError, ValueError):
        prev_f = None
    extreme = _update_extreme(side_is_long, mark, prev_f)
    state["pullback_extreme"] = extreme
    state["phase"] = "pullback"
    state["symbol"] = symbol.upper()
    state["is_long"] = side_is_long
    state["entry"] = float(entry)

    gross = profit_pct(entry, mark, side_is_long)
    net = estimated_net_pct(gross, fee_buf)
    giveback = _giveback_pct(side_is_long, extreme, mark)
    profit_ok = gross >= min_pct and net > 0
    # Extreme must have extended beyond entry (real favorable excursion)
    if side_is_long:
        extended = extreme > entry
    else:
        extended = extreme < entry

    if not (profit_ok and extended and giveback >= pb):
        need = []
        if not profit_ok:
            need.append(f"pnl≥{min_pct:g}% net>0 (now {gross:+.3f}% net≈{net:+.3f}%)")
        if not extended:
            need.append("wait favorable extreme")
        elif giveback < pb:
            need.append(f"giveback {giveback:.3f}% < {pb:g}%")
        print(
            f"{grid.DIM}Pullback armed · {side} ext={grid.price_fmt(extreme)} "
            f"mark={grid.price_fmt(mark)} · {' · '.join(need) or 'tracking'}{grid.RESET}"
        )
        staged.save_state(symbol.upper(), state)
        return

    reason = (
        f"pullback {giveback:.3f}% from ext {grid.price_fmt(extreme)} "
        f"(≥{pb:g}%)"
    )
    print(
        f"{grid.GREEN}✓ Pullback TP {side} · {reason} · "
        f"pnl={gross:+.3f}% net≈{net:+.3f}% "
        f"@ {grid.price_fmt(mark)}{grid.RESET}"
    )
    if bool(getattr(args, "dry_run", False)):
        _CLOSE_REASONS[symbol.upper()] = f"Pullback · {reason}"
        staged.save_state(symbol.upper(), state)
        return

    try:
        try:
            grid.cancel_all_symbol_orders(symbol, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel open orders: {exc}{grid.RESET}")
        cancel_close_algos(symbol, side_is_long, api, sec, recv)
        closed = grid.market_close_position(
            symbol, side_is_long, qty, hedge, filt, api, sec, recv,
        )
        print(f"{grid.GREEN}✓ Market-closed {closed:g} ({reason}){grid.RESET}")
        _CLOSE_REASONS[symbol.upper()] = f"Pullback · {reason}"
        state["pullback_extreme"] = None
        staged.save_state(symbol.upper(), state)
        time.sleep(0.35)
    except Exception as exc:
        print(f"{grid.RED}✗ Pullback close failed: {exc}{grid.RESET}")
        staged.save_state(symbol.upper(), state)
