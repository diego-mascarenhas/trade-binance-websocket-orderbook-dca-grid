"""Exchange-side TP/SL for OB scalp (Binance Futures algo orders).

Places TAKE_PROFIT_MARKET + STOP_MARKET at open so exits survive bot/pick handoff.
Uses clientAlgoId prefix ``obscalp`` (distinct from DCA ``obstage``).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from orderbook_dca_grid import (
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    _dec_places,
    _round_to,
    _signed_request,
    price_fmt,
)

ALGO_PREFIX = "obscalp"
TAGS = ("TP", "SL")


def _algo_cid(tag: str, symbol: str) -> str:
    return f"{ALGO_PREFIX}{tag}{symbol.upper()}"


def _algo_client_id(order: dict) -> str:
    return str(order.get("clientAlgoId") or order.get("newClientOrderId") or "")


def list_open_algo_orders(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    try:
        resp = _signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv,
        )
    except Exception:
        return []
    if isinstance(resp, list):
        return resp
    return list(resp.get("orders") or resp.get("data") or [])


def _cancel_error_benign(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        x in text
        for x in ("-2011", "-2013", "unknown order", "already been canceled", "does not exist")
    )


def cancel_algo_order(symbol: str, algo_id: int | str, api: str, sec: str, recv: int) -> bool:
    try:
        _signed_request(
            "DELETE", "/fapi/v1/algoOrder",
            {"symbol": symbol.upper(), "algoId": algo_id},
            api, sec, recv,
        )
        return True
    except Exception as exc:
        if _cancel_error_benign(exc):
            return False
        raise


def cancel_scalp_exchange_exits(symbol: str, api: str, sec: str, recv: int) -> int:
    """Cancel our open scalp TP/SL algo orders. Returns count cancelled."""
    killed = 0
    sym = symbol.upper()
    for o in list_open_algo_orders(symbol, api, sec, recv):
        cid = _algo_client_id(o)
        if not (cid.startswith(ALGO_PREFIX) and cid.endswith(sym)):
            continue
        if cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
            killed += 1
    return killed


def our_exits_present(symbol: str, api: str, sec: str, recv: int) -> tuple[bool, bool]:
    """Return (has_tp, has_sl) for our scalp algo tags."""
    has_tp = has_sl = False
    for o in list_open_algo_orders(symbol, api, sec, recv):
        cid = _algo_client_id(o)
        if cid == _algo_cid("TP", symbol):
            has_tp = True
        elif cid == _algo_cid("SL", symbol):
            has_sl = True
    return has_tp, has_sl


def _stop_would_fire(is_long: bool, trigger: float, mark: float, tick: Decimal) -> bool:
    tol = float(tick) * 0.5
    return mark <= trigger + tol if is_long else mark >= trigger - tol


def _tp_would_fire(is_long: bool, trigger: float, mark: float, tick: Decimal) -> bool:
    tol = float(tick) * 0.5
    return mark >= trigger - tol if is_long else mark <= trigger + tol


def _nudge_away(
    is_long: bool,
    *,
    is_tp: bool,
    trigger: float,
    mark: float,
    tick: Decimal,
    fee_buffer_pct: float,
) -> float:
    """Push trigger off the mark so placement does not immediately fire."""
    pad = max(float(tick) * 2, mark * max(fee_buffer_pct, 0.05) / 100.0)
    if is_tp:
        return mark + pad if is_long else mark - pad
    return mark - pad if is_long else mark + pad


def _place_one(
    symbol: str,
    is_long: bool,
    order_type: str,
    qty_str: str,
    trigger_str: str,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    tag: str,
) -> dict:
    side = "SELL" if is_long else "BUY"
    params: dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": side,
        "type": order_type,
        "quantity": qty_str,
        "triggerPrice": trigger_str,
        "workingType": "CONTRACT_PRICE",
        "clientAlgoId": _algo_cid(tag, symbol),
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def place_scalp_exchange_exits(
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
    replace: bool = True,
) -> tuple[float, float]:
    """Place (or replace) TAKE_PROFIT_MARKET + STOP_MARKET. Returns (tp, sl) used."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    qty_d = _round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        raise ValueError("qty too small for exchange exits")
    qty_str = f"{qty_d:.{qty_dp}f}"

    tp = float(tp_price)
    sl = float(sl_price)
    if tp <= 0 or sl <= 0:
        raise ValueError("tp/sl prices required")

    if _tp_would_fire(is_long, tp, mark, tick):
        tp = _nudge_away(is_long, is_tp=True, trigger=tp, mark=mark, tick=tick, fee_buffer_pct=fee_buffer_pct)
        print(f"{YELLOW}TP was at/through mark — nudged to {price_fmt(tp)}{RESET}")
    if _stop_would_fire(is_long, sl, mark, tick):
        sl = _nudge_away(is_long, is_tp=False, trigger=sl, mark=mark, tick=tick, fee_buffer_pct=fee_buffer_pct)
        print(f"{YELLOW}SL was at/through mark — nudged to {price_fmt(sl)}{RESET}")

    # Round away from entry so we do not tighten accidentally
    if is_long:
        tp_d = _round_to(tp, tick, ROUND_UP)
        sl_d = _round_to(sl, tick, ROUND_DOWN)
    else:
        tp_d = _round_to(tp, tick, ROUND_DOWN)
        sl_d = _round_to(sl, tick, ROUND_UP)
    tp_str = f"{tp_d:.{price_dp}f}"
    sl_str = f"{sl_d:.{price_dp}f}"

    if replace:
        cancel_scalp_exchange_exits(symbol, api, sec, recv)

    close_side = "SELL" if is_long else "BUY"
    try:
        tp_resp = _place_one(
            symbol, is_long, "TAKE_PROFIT_MARKET", qty_str, tp_str,
            hedge, api, sec, recv, tag="TP",
        )
        print(
            f"{GREEN}✓ Exchange TP {close_side} {qty_str} @ {tp_str} "
            f"(algoId={tp_resp.get('algoId')}){RESET}",
        )
    except Exception as exc:
        print(f"{RED}✗ Exchange TP failed: {exc}{RESET}")
        raise

    try:
        sl_resp = _place_one(
            symbol, is_long, "STOP_MARKET", qty_str, sl_str,
            hedge, api, sec, recv, tag="SL",
        )
        print(
            f"{GREEN}✓ Exchange SL {close_side} {qty_str} @ {sl_str} "
            f"(algoId={sl_resp.get('algoId')}){RESET}",
        )
    except Exception as exc:
        print(f"{RED}✗ Exchange SL failed: {exc}{RESET}")
        # Best-effort: leave TP up; caller may retry SL
        print(f"{YELLOW}TP left on book — retry SL or close manually if needed{RESET}")
        raise

    print(f"{DIM}Exchange exits armed · TP {tp_str} · SL {sl_str}{RESET}")
    return float(tp_d), float(sl_d)
