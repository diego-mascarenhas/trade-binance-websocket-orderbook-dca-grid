"""Exit: close in profit when opposing EQH/EQL structure is touched.

LONG  → close when near EQH (after already green)
SHORT → close when near EQL (after already green)

No exchange TAKE_PROFIT is placed; the supervise loop soft-closes on signal.
"""

from __future__ import annotations

import argparse
import time
from decimal import Decimal

from ob_structure import (
    fetch_structure,
    should_structure_tp,
    structure_config_from_args,
    structure_tp_level,
)
from ob_signals import estimated_net_pct, profit_pct

# Last structure-TP close reason per symbol (supervise reads it when flat).
_CLOSE_REASONS: dict[str, str] = {}


def pop_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.pop(symbol.upper(), None)


def peek_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.get(symbol.upper())


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

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    fee_buf = float(getattr(args, "tp_fee_buffer", 0.12) or 0.0)

    try:
        depth = grid.fetch_depth(symbol, getattr(args, "limit", 50))
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        mark = (float(bids[0][0]) + float(asks[0][0])) / 2.0 if bids and asks else 0.0
    except Exception as exc:
        print(f"{grid.YELLOW}Structure TP depth skip: {exc}{grid.RESET}")
        return
    if mark <= 0 or entry <= 0 or qty <= 0:
        return

    gross = profit_pct(entry, mark, side_is_long)
    # Must already be in profit (gross > 0) and estimated net stay green.
    in_profit = gross > 0 and estimated_net_pct(gross, fee_buf) > 0
    cfg = structure_config_from_args(args)
    try:
        snap = fetch_structure(symbol, cfg=cfg)
    except Exception as exc:
        print(f"{grid.YELLOW}Structure fetch skip: {exc}{grid.RESET}")
        return

    fire, reason = should_structure_tp(side_is_long, in_profit=in_profit, snap=snap)
    lvl = structure_tp_level(side_is_long, snap)
    side = "LONG" if side_is_long else "SHORT"
    if not fire:
        # Quiet status (same cadence as other exit plugins)
        need = "EQH" if side_is_long else "EQL"
        profit_note = f"pnl={gross:+.3f}% net≈{gross - fee_buf:+.3f}%"
        if not in_profit:
            print(
                f"{grid.DIM}Structure TP wait · {side} not green yet "
                f"({profit_note}) · need {need}{grid.RESET}"
            )
        else:
            far = ""
            if side_is_long and snap.eqh_level > 0 and not snap.eqh:
                far = f" EQH@{snap.eqh_level:g} (far)"
            elif (not side_is_long) and snap.eql_level > 0 and not snap.eql:
                far = f" EQL@{snap.eql_level:g} (far)"
            print(
                f"{grid.DIM}Structure TP armed · {side} {profit_note} · "
                f"waiting {need}{far} · {snap.reason}{grid.RESET}"
            )
        return

    print(
        f"{grid.GREEN}✓ Structure TP {side} · {reason} · "
        f"pnl={gross:+.3f}% @ {grid.price_fmt(mark)}"
        f"{f' (lvl {grid.price_fmt(lvl)})' if lvl > 0 else ''}{grid.RESET}"
    )
    try:
        # Flatten exits + open limits before market close to avoid race fills.
        try:
            grid.cancel_all_symbol_orders(symbol, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel open orders: {exc}{grid.RESET}")
        _cancel_close_algos(symbol, side_is_long, api, sec, recv)
        closed = grid.market_close_position(
            symbol, side_is_long, qty, hedge, filt, api, sec, recv,
        )
        print(f"{grid.GREEN}✓ Market-closed {closed:g} ({reason}){grid.RESET}")
        _CLOSE_REASONS[symbol.upper()] = f"structure TP · {reason}"
        time.sleep(0.35)
    except Exception as exc:
        print(f"{grid.RED}✗ Structure TP close failed: {exc}{grid.RESET}")


def cancel_close_algos(
    symbol: str,
    is_long: bool,
    api: str,
    sec: str,
    recv: int,
) -> int:
    """Cancel any reduce-side conditional TP/SL/trail algos (exit presets)."""
    import orderbook_dca_grid as grid

    close_side = "SELL" if is_long else "BUY"
    exit_types = {
        "TRAILING_STOP_MARKET", "STOP_MARKET", "STOP",
        "TAKE_PROFIT_MARKET", "TAKE_PROFIT",
    }
    killed = 0
    try:
        resp = grid._signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv,
        )
    except Exception:
        return 0
    orders = resp if isinstance(resp, list) else (resp or {}).get("orders", (resp or {}).get("data", []))
    for o in orders or []:
        otype = str(o.get("orderType") or o.get("type") or "").upper()
        if otype not in exit_types:
            continue
        if str(o.get("side", "")).upper() != close_side:
            continue
        try:
            grid._signed_request(
                "DELETE", "/fapi/v1/algoOrder",
                {"symbol": symbol.upper(), "algoId": o.get("algoId")},
                api, sec, recv,
            )
            killed += 1
        except Exception:
            pass
    return killed


def _cancel_close_algos(
    symbol: str,
    is_long: bool,
    api: str,
    sec: str,
    recv: int,
) -> int:
    """Backward-compatible alias."""
    return cancel_close_algos(symbol, is_long, api, sec, recv)
