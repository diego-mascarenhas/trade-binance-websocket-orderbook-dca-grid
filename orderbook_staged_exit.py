#!/usr/bin/env python3
"""Experimental staged exit manager for Binance Futures (addon to the DCA grid bot).

Runs alongside orderbook_dca_grid.py on test pairs. When a position is open:
  1. Places TP1 (70%) as TAKE_PROFIT at +TP1_PROFIT_PCT (default 0.3%) — DCA grid stays active
  2. On TP1 fill: cancels DCA, SL on runner at original entry
  3. Trailing on the opposite order-book wall for the runner

Default mode is automatic (supervise loop). Or use the main bot:
  python3 orderbook_dca_grid.py SYMBOL --supervise --exit staged
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

FAPI_BASE = os.getenv("FAPI_BASE", "https://fapi.binance.com").rstrip("/")
ALGO_PREFIX = "obstage"
STATE_DIRNAME = ".state"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

PHASE_IDLE = "idle"
PHASE_WAITING = "waiting_profit"
PHASE_TP1 = "tp1_armed"
PHASE_PARTIAL = "staged_partial"
PHASE_TRAIL = "staged_trail"

STAGED_TAGS = ("TP1", "BE", "TR", "SL")
ALLOWED_ALGOS_BY_PHASE: dict[str, set[str]] = {
    PHASE_TP1: {"TP1"},
    PHASE_PARTIAL: {"BE"},
    PHASE_TRAIL: {"TR"},
}
# legacy alias
PHASE_FULL = PHASE_WAITING


# --- Shared utilities (forked from orderbook_dca_grid.py) -----------------


def fetch_depth(symbol: str, limit: int) -> dict:
    url = f"{FAPI_BASE}/fapi/v1/depth?symbol={symbol.upper()}&limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def price_fmt(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.7f}"


def qty_fmt(qty: float) -> str:
    if qty >= 1000:
        return f"{qty:,.1f}"
    if qty >= 1:
        return f"{qty:,.2f}"
    return f"{qty:.4f}"


def load_env_file(env_file: str | None) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [env_file, os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")]
    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


def load_keys(env_file: str | None) -> tuple[str, str]:
    load_env_file(env_file)
    return os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", "")


def _public_get(path: str, params: dict) -> dict:
    url = f"{FAPI_BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _signed_request(method: str, path: str, params: dict, api: str, sec: str, recv_window: int) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window
    query = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_BASE}{path}?{query}&signature={sig}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": api})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None


def load_symbol_filters(symbol: str) -> dict[str, Decimal]:
    info = _public_get("/fapi/v1/exchangeInfo", {})
    filt = {
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
    }
    for item in info.get("symbols", []):
        if item.get("symbol") != symbol.upper():
            continue
        for f in item.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                filt["tick_size"] = Decimal(str(f.get("tickSize", "0.01")))
            elif f.get("filterType") == "LOT_SIZE":
                filt["step_size"] = Decimal(str(f.get("stepSize", "0.001")))
                filt["min_qty"] = Decimal(str(f.get("minQty", "0.001")))
            elif f.get("filterType") == "MIN_NOTIONAL":
                filt["min_notional"] = Decimal(str(f.get("notional", "5")))
        break
    return filt


def _dec_places(step: Decimal) -> int:
    exp = step.normalize().as_tuple().exponent
    return max(0, -exp)


def _round_to(value: float, step: Decimal, rounding: str) -> Decimal:
    return (Decimal(str(value)) / step).to_integral_value(rounding=rounding) * step


def _order_client_id(order: dict) -> str:
    return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")


def _algo_client_id(order: dict) -> str:
    return str(order.get("clientAlgoId") or order.get("newClientOrderId") or "")


def choose_tp_activation(
    bids: list[list[float]],
    asks: list[list[float]],
    avg: float,
    latest: float,
    is_long: bool,
    callback: float,
    fee_buffer: float,
    tick: Decimal,
    wall_min_mult: float,
    pick: str,
) -> dict:
    all_q = [q for _, q in bids] + [q for _, q in asks]
    med = statistics.median(all_q) if all_q else 0.0
    min_wall = med * wall_min_mult
    tickf = float(tick)

    if not is_long:
        threshold = avg * (1 - fee_buffer / 100) / (1 + callback / 100)
        cands = [(p, q) for p, q in bids if p <= threshold]
        strong = [(p, q) for p, q in cands if q >= min_wall]
        wall = None
        if strong:
            wall = max(strong, key=lambda x: x[0]) if pick == "nearest" else max(strong, key=lambda x: x[1])
            activation = wall[0]
        else:
            activation = threshold
        activation = min(activation, latest - 2 * tickf)
        activation = float(_round_to(activation, tick, ROUND_DOWN))
        worst_exit = activation * (1 + callback / 100)
        profit_worst = (avg - worst_exit) / avg * 100
    else:
        threshold = avg * (1 + fee_buffer / 100) / (1 - callback / 100)
        cands = [(p, q) for p, q in asks if p >= threshold]
        strong = [(p, q) for p, q in cands if q >= min_wall]
        wall = None
        if strong:
            wall = min(strong, key=lambda x: x[0]) if pick == "nearest" else max(strong, key=lambda x: x[1])
            activation = wall[0]
        else:
            activation = threshold
        activation = max(activation, latest + 2 * tickf)
        activation = float(_round_to(activation, tick, ROUND_UP))
        worst_exit = activation * (1 - callback / 100)
        profit_worst = (worst_exit - avg) / avg * 100

    return {
        "activation": activation,
        "wall_qty": wall[1] if wall else None,
        "on_wall": wall is not None,
        "profit_worst_pct": profit_worst,
    }


def choose_sl_price(
    bids: list[list[float]],
    asks: list[list[float]],
    entry: float,
    is_long: bool,
    sl_pct: float,
    sl_wall: bool,
    tick: Decimal,
    wall_min_mult: float,
) -> float:
    """Adverse stop price: % from entry, or an adverse OB wall when --sl-wall."""
    if sl_wall:
        all_q = [q for _, q in bids] + [q for _, q in asks]
        med = statistics.median(all_q) if all_q else 0.0
        min_wall = med * wall_min_mult
        if is_long:
            threshold = entry * (1 - sl_pct / 100)
            cands = [(p, q) for p, q in bids if p <= threshold]
            strong = [(p, q) for p, q in cands if q >= min_wall]
            if strong:
                return float(_round_to(min(strong, key=lambda x: x[0])[0], tick, ROUND_DOWN))
        else:
            threshold = entry * (1 + sl_pct / 100)
            cands = [(p, q) for p, q in asks if p >= threshold]
            strong = [(p, q) for p, q in cands if q >= min_wall]
            if strong:
                return float(_round_to(max(strong, key=lambda x: x[0])[0], tick, ROUND_UP))
    if is_long:
        return float(_round_to(entry * (1 - sl_pct / 100), tick, ROUND_DOWN))
    return float(_round_to(entry * (1 + sl_pct / 100), tick, ROUND_UP))


def breakeven_price(entry: float, is_long: bool, fee_buffer: float, tick: Decimal) -> float:
    if is_long:
        return float(_round_to(entry * (1 + fee_buffer / 100), tick, ROUND_DOWN))
    return float(_round_to(entry * (1 - fee_buffer / 100), tick, ROUND_UP))


def entry_stop_price(entry: float, is_long: bool, tick: Decimal) -> float:
    """SL trigger at the original entry (breakeven on the runner after partial TP)."""
    if is_long:
        return float(_round_to(entry, tick, ROUND_DOWN))
    return float(_round_to(entry, tick, ROUND_UP))


def profit_pct(entry: float, mark: float, is_long: bool) -> float:
    if entry <= 0:
        return 0.0
    if is_long:
        return (mark - entry) / entry * 100.0
    return (entry - mark) / entry * 100.0


def profit_target_price(entry: float, is_long: bool, target_pct: float, tick: Decimal) -> float:
    if is_long:
        return float(_round_to(entry * (1 + target_pct / 100), tick, ROUND_UP))
    return float(_round_to(entry * (1 - target_pct / 100), tick, ROUND_DOWN))


def profit_target_hit(entry: float, mark: float, is_long: bool, target_pct: float) -> bool:
    return profit_pct(entry, mark, is_long) >= target_pct


def trail_wall_plan(
    symbol: str, entry: float, is_long: bool, args: argparse.Namespace, tick: Decimal,
) -> dict:
    depth = fetch_depth(symbol, args.limit)
    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]
    latest = (bids[0][0] + asks[0][0]) / 2
    return choose_tp_activation(
        bids, asks, entry, latest, is_long, args.tp_callback,
        args.tp_fee_buffer, tick, args.tp_wall_min_mult, args.tp_wall_pick,
    )


def get_position(symbol: str, is_long: bool, hedge: bool, api: str, sec: str, recv: int) -> tuple[float, float]:
    rows = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, api, sec, recv)
    want_side = ("LONG" if is_long else "SHORT") if hedge else "BOTH"
    for r in rows if isinstance(rows, list) else []:
        if str(r.get("positionSide", "BOTH")).upper() != want_side:
            continue
        amt = float(r.get("positionAmt", 0) or 0)
        if abs(amt) > 0:
            return abs(amt), float(r.get("entryPrice", 0) or 0)
    return 0.0, 0.0


def position_pnl(
    symbol: str, is_long: bool, hedge: bool, api: str, sec: str, recv: int,
) -> tuple[float, float]:
    """Return (notional_usdt, unrealized_pnl) for the open position side."""
    rows = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, api, sec, recv)
    want_side = ("LONG" if is_long else "SHORT") if hedge else "BOTH"
    for r in rows if isinstance(rows, list) else []:
        if str(r.get("positionSide", "BOTH")).upper() != want_side:
            continue
        amt = float(r.get("positionAmt", 0) or 0)
        if abs(amt) <= 0:
            continue
        entry = float(r.get("entryPrice", 0) or 0)
        mark = float(r.get("markPrice", 0) or 0)
        raw_n = r.get("notional", "")
        if raw_n not in ("", None):
            notional = abs(float(raw_n))
        else:
            notional = abs(amt) * (mark if mark > 0 else entry)
        return notional, float(r.get("unRealizedProfit", 0) or 0)
    return 0.0, 0.0


def get_symbol_leverage(symbol: str, api: str, sec: str, recv: int) -> int:
    rows = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, api, sec, recv)
    for r in rows if isinstance(rows, list) else []:
        lev = int(float(r.get("leverage", 0) or 0))
        if lev > 0:
            return lev
    return 10


def _detect_open_side(
    symbol: str, hedge: bool, api: str, sec: str, recv: int,
    prefer_is_long: bool | None = None,
) -> tuple[bool | None, float, float]:
    order = (True, False) if prefer_is_long is None else (prefer_is_long, not prefer_is_long)
    for want_long in order:
        q, e = get_position(symbol, want_long, hedge, api, sec, recv)
        if q > 0:
            return want_long, q, e
    return None, 0.0, 0.0


def get_mark_price(symbol: str, api: str, sec: str, recv: int) -> float:
    idx = _signed_request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol.upper()}, api, sec, recv)
    return float(idx.get("markPrice", 0) or 0)


def _resolve_hedge(args: argparse.Namespace, api: str, sec: str) -> bool:
    if args.position_mode == "hedge":
        return True
    if args.position_mode == "oneway":
        return False
    try:
        resp = _signed_request("GET", "/fapi/v1/positionSide/dual", {}, api, sec, args.recv_window)
        return bool(resp.get("dualSidePosition"))
    except Exception:
        return False


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- State persistence -----------------------------------------------------


def _state_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, STATE_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def state_path(symbol: str) -> str:
    return os.path.join(_state_dir(), f"{symbol.upper()}_staged.json")


def load_state(symbol: str) -> dict[str, Any]:
    path = state_path(symbol)
    if not os.path.exists(path):
        return {"phase": PHASE_IDLE, "symbol": symbol.upper()}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"phase": PHASE_IDLE, "symbol": symbol.upper()}


def save_state(symbol: str, state: dict[str, Any]) -> None:
    state["symbol"] = symbol.upper()
    state["updated_at"] = int(time.time())
    with open(state_path(symbol), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def clear_state(symbol: str) -> None:
    path = state_path(symbol)
    if os.path.exists(path):
        os.remove(path)


# --- Algo orders -----------------------------------------------------------


def list_open_algo_orders(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    try:
        resp = _signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv)
    except Exception:
        return []
    if isinstance(resp, list):
        return resp
    return resp.get("orders", resp.get("data", [])) or []


def _cancel_error_benign(exc: BaseException) -> bool:
    """True when the algo is already gone (UI may still show a ghost line)."""
    text = str(exc).lower()
    return any(
        x in text
        for x in ("-2011", "-2013", "unknown order", "already been canceled", "does not exist")
    )


def _staged_tag_from_cid(cid: str, symbol: str) -> str | None:
    sym = symbol.upper()
    if not cid.startswith(ALGO_PREFIX):
        return None
    rest = cid[len(ALGO_PREFIX):]
    if rest.endswith(sym):
        return rest[:-len(sym)]
    return None


def cancel_algo_order(symbol: str, algo_id: int | str, api: str, sec: str, recv: int) -> bool:
    """Cancel one algo order. Returns False if already gone (benign)."""
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


def cancel_our_algos(symbol: str, tag: str, api: str, sec: str, recv: int) -> int:
    """Cancel our algo orders matching clientAlgoId obstage{tag}{symbol}."""
    want = _algo_client_tag(tag, symbol)
    killed = 0
    for o in list_open_algo_orders(symbol, api, sec, recv):
        cid = _algo_client_id(o)
        if cid == want or cid.startswith(want):
            if cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
                killed += 1
            else:
                print(f"{DIM}Algo {cid} already gone (chart may show ghost until refresh).{RESET}")
    return killed


def cancel_all_staged_algos(symbol: str, api: str, sec: str, recv: int) -> int:
    """Cancel every open obstage* conditional order on the symbol."""
    killed = 0
    for tag in STAGED_TAGS:
        killed += cancel_our_algos(symbol, tag, api, sec, recv)
    return killed


def reconcile_staged_algos(
    symbol: str,
    phase: str,
    api: str,
    sec: str,
    recv: int,
) -> int:
    """Drop stray/duplicate staged algos that do not match the current phase."""
    allowed = ALLOWED_ALGOS_BY_PHASE.get(phase, set())
    by_tag: dict[str, list[dict]] = {}
    for o in list_open_algo_orders(symbol, api, sec, recv):
        tag = _staged_tag_from_cid(_algo_client_id(o), symbol)
        if tag is None:
            continue
        by_tag.setdefault(tag, []).append(o)

    killed = 0
    for tag, orders in by_tag.items():
        if tag not in allowed:
            for o in orders:
                cid = _algo_client_id(o)
                if cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
                    print(f"{YELLOW}Removed stray {tag} algo {cid} (phase={phase}).{RESET}")
                    killed += 1
            continue
        if len(orders) > 1:
            orders.sort(key=lambda x: int(x.get("algoId") or 0))
            for o in orders[:-1]:
                cid = _algo_client_id(o)
                if cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
                    print(f"{YELLOW}Removed duplicate {tag} algo {cid}.{RESET}")
                    killed += 1
    return killed


def cancel_legacy_exit_algos(symbol: str, is_long: bool, api: str, sec: str, recv: int) -> int:
    """Cancel trailing/TP/SL algos from the old DCA bot (not obstage*)."""
    close_side = "SELL" if is_long else "BUY"
    killed = 0
    for o in list_open_algo_orders(symbol, api, sec, recv):
        cid = _algo_client_id(o)
        if cid.startswith(ALGO_PREFIX):
            continue
        otype = str(o.get("orderType") or o.get("type") or "").upper()
        if str(o.get("side", "")).upper() != close_side:
            continue
        if otype in ("TRAILING_STOP_MARKET", "STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            try:
                if cancel_algo_order(symbol, o.get("algoId"), api, sec, recv):
                    print(f"{DIM}Cancelled legacy {otype} algoId={o.get('algoId')}{RESET}")
                    killed += 1
            except Exception as exc:
                print(f"{RED}Cancel legacy {otype} failed: {exc}{RESET}")
    return killed


def cancel_dca_grid_orders(symbol: str, api: str, sec: str, recv: int) -> int:
    """Cancel obdca* limit orders placed by orderbook_dca_grid.py."""
    from orderbook_dca_grid import cancel_dca_grid_orders as _cancel_grid

    return _cancel_grid(symbol, api, sec, recv)


def _algo_client_tag(tag: str, symbol: str) -> str:
    return f"{ALGO_PREFIX}{tag}{symbol.upper()}"


def place_algo_order(
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
    client_tag: str,
    callback: float | None = None,
    activate_str: str | None = None,
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
        "clientAlgoId": _algo_client_tag(client_tag, symbol),
    }
    if order_type == "TRAILING_STOP_MARKET":
        params["callbackRate"] = callback
        if activate_str:
            params["activatePrice"] = activate_str
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def find_our_algo(symbol: str, tag: str, api: str, sec: str, recv: int) -> dict | None:
    want = _algo_client_tag(tag, symbol)
    for o in list_open_algo_orders(symbol, api, sec, recv):
        if _algo_client_id(o) == want:
            return o
    return None


def split_partial_qty(
    qty: float, partial_pct: float, step: Decimal, min_qty: Decimal, min_notional: Decimal, ref_price: float,
) -> tuple[Decimal, Decimal]:
    """Return (tp1_qty, remain_qty) rounded to step, respecting min qty/notional."""
    total = _round_to(qty, step, ROUND_DOWN)
    if total <= 0:
        return Decimal("0"), Decimal("0")
    tp1 = _round_to(float(total) * partial_pct / 100.0, step, ROUND_DOWN)
    remain = total - tp1
    if tp1 < min_qty:
        tp1 = min_qty
        remain = total - tp1
    if remain < min_qty:
        tp1 = total - min_qty
        remain = min_qty
    if tp1 <= 0 or remain <= 0:
        return total, Decimal("0")
    while tp1 * Decimal(str(ref_price)) < min_notional and tp1 < total:
        tp1 += step
        remain = total - tp1
    while remain * Decimal(str(ref_price)) < min_notional and remain > 0:
        remain -= step
        tp1 = total - remain
    if tp1 <= 0 or remain <= 0:
        return total, Decimal("0")
    return tp1, remain


# --- Staged exit logic -----------------------------------------------------


def _qty_strings(tp1: Decimal, remain: Decimal, qty_dp: int) -> tuple[str, str]:
    return f"{tp1:.{qty_dp}f}", f"{remain:.{qty_dp}f}"


def _is_immediate_trigger_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "-2021" in text or "immediately trigger" in text


def _stop_would_immediately_trigger(is_long: bool, trigger: float, mark: float, tick: Decimal) -> bool:
    """STOP_MARKET that closes the position would fire on placement."""
    tol = float(tick) * 0.5
    if is_long:
        return mark <= trigger + tol
    return mark >= trigger - tol


def _tp_would_immediately_trigger(is_long: bool, trigger: float, mark: float, tick: Decimal) -> bool:
    """TAKE_PROFIT_MARKET that closes the position would fire on placement."""
    tol = float(tick) * 0.5
    if is_long:
        return mark >= trigger - tol
    return mark <= trigger + tol


def _market_reduce_qty(
    symbol: str,
    is_long: bool,
    qty: Decimal,
    hedge: bool,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
    recv: int,
) -> float:
    """Reduce-only MARKET close for a partial quantity."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    qty_d = _round_to(float(qty), step, ROUND_DOWN)
    if qty_d <= 0:
        return 0.0
    side = "SELL" if is_long else "BUY"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": f"{qty_d:.{qty_dp}f}",
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)
    return float(qty_d)


def _execute_tp1_market(
    symbol: str,
    is_long: bool,
    qty: float,
    state: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    tp1_d: Decimal,
    remain_d: Decimal,
    tp1_trig_f: float,
    reason: str = "",
) -> dict[str, Any]:
    """Close TP1 portion at market when the profit target is already reached."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    tp1_str, _ = _qty_strings(tp1_d, remain_d, qty_dp)
    close_side = "SELL" if is_long else "BUY"
    prefix = f"{reason} " if reason else ""
    mark = get_mark_price(symbol, api, sec, args.recv_window)

    print(f"{prefix}{YELLOW}Profit target already hit (mark {price_fmt(mark)}) — "
          f"{close_side} MARKET {tp1_str} instead of conditional TP1{RESET}")

    wall = trail_wall_plan(symbol, float(state.get("entry_anchor", state.get("entry", 0)) or 0),
                           is_long, args, filt["tick_size"])
    armed = float(_round_to(qty, step, ROUND_DOWN))
    state.update({
        "phase": PHASE_TP1,
        "tp1_price": tp1_trig_f,
        "tp1_qty": float(tp1_d),
        "remain_qty": float(remain_d),
        "armed_qty": armed,
        "trail_wall_price": wall["activation"],
    })
    if "pre_armed_qty" not in state or float(state.get("pre_armed_qty", 0) or 0) < armed:
        state["pre_armed_qty"] = armed

    if args.dry_run:
        state["algo_ids"] = {}
        return state

    cancel_all_staged_algos(symbol, api, sec, args.recv_window)
    _market_reduce_qty(symbol, is_long, tp1_d, hedge, filt, api, sec, args.recv_window)

    _, runner_qty, _ = _detect_open_side(symbol, hedge, api, sec, args.recv_window, is_long)
    runner = float(runner_qty) if runner_qty > 0 else float(remain_d)
    return _transition_to_partial(symbol, is_long, runner, state, args, hedge, api, sec, filt)


def place_tp1_order(
    symbol: str,
    is_long: bool,
    qty: float,
    state: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Place (or replace) TP1 partial at the profit target price. DCA grid is kept."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    entry = float(state.get("entry_anchor", state.get("entry", 0)) or 0)
    tp1_trig_f = profit_target_price(entry, is_long, args.tp1_profit_pct, tick)
    tp1_trig = f"{tp1_trig_f:.{price_dp}f}"

    wall = trail_wall_plan(symbol, entry, is_long, args, tick)
    tp1_d, remain_d = split_partial_qty(
        qty, args.tp_partial_pct, step, filt["min_qty"], filt["min_notional"], tp1_trig_f,
    )
    tp1_str, remain_str = _qty_strings(tp1_d, remain_d, qty_dp)
    close_side = "SELL" if is_long else "BUY"
    prefix = f"{reason} " if reason else ""

    print(f"{prefix}{close_side} TAKE_PROFIT_MARKET {tp1_str} ({args.tp_partial_pct:g}%) @ {tp1_trig} "
          f"(+{args.tp1_profit_pct:g}% from entry)")

    mark = get_mark_price(symbol, api, sec, args.recv_window)
    if profit_target_hit(entry, mark, is_long, args.tp1_profit_pct):
        return _execute_tp1_market(
            symbol, is_long, qty, state, args, hedge, api, sec, filt,
            tp1_d=tp1_d, remain_d=remain_d, tp1_trig_f=tp1_trig_f, reason=prefix.strip(),
        )

    if not args.dry_run:
        cancel_all_staged_algos(symbol, api, sec, args.recv_window)
        try:
            tp1_resp = place_algo_order(
                symbol, is_long, "TAKE_PROFIT_MARKET", tp1_str, tp1_trig,
                hedge, api, sec, args.recv_window, client_tag="TP1",
            )
            print(f"{GREEN}✓ TP1 algoId={tp1_resp.get('algoId')}{RESET}")
            state["algo_ids"] = {"tp1": tp1_resp.get("algoId")}
        except RuntimeError as exc:
            if _is_immediate_trigger_error(exc):
                print(f"{YELLOW}TP1 conditional rejected (-2021) — falling back to market partial{RESET}")
                return _execute_tp1_market(
                    symbol, is_long, qty, state, args, hedge, api, sec, filt,
                    tp1_d=tp1_d, remain_d=remain_d, tp1_trig_f=tp1_trig_f,
                )
            raise
    else:
        state["algo_ids"] = {}

    state.update({
        "phase": PHASE_TP1,
        "tp1_price": tp1_trig_f,
        "tp1_qty": float(tp1_d),
        "remain_qty": float(remain_d),
        "armed_qty": float(_round_to(qty, step, ROUND_DOWN)),
        "trail_wall_price": wall["activation"],
    })
    if "pre_armed_qty" not in state:
        state["pre_armed_qty"] = float(_round_to(qty, step, ROUND_DOWN))
    return state


def _tp1_qty_needs_sync(state: dict[str, Any], qty: float, step: Decimal) -> bool:
    """True when DCA increased position size — not when TP1 partial reduced it."""
    armed = float(state.get("armed_qty", 0) or 0)
    if armed <= 0:
        return True
    tol = float(step) / 2
    if qty < armed - tol:
        return False
    return qty > armed + tol


def arm_staged_exit(
    symbol: str,
    is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> dict[str, Any]:
    """Attach to an open position: place TP1 at profit target (DCA stays until TP1 fills)."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    total_d = _round_to(qty, step, ROUND_DOWN)
    tp1_target = profit_target_price(entry, is_long, args.tp1_profit_pct, tick)
    wall = trail_wall_plan(symbol, entry, is_long, args, tick)

    print(f"\n{BOLD}{CYAN}Armed {symbol.upper()} {'LONG' if is_long else 'SHORT'} "
          f"qty {total_d} @ {price_fmt(entry)}{RESET}")
    print(f"  TP1 {args.tp_partial_pct:g}% @ {price_fmt(tp1_target)} (+{args.tp1_profit_pct:g}% profit)")
    print(f"  Then SL runner @ entry {price_fmt(entry)} · trailing wall @ {price_fmt(wall['activation'])}")
    print(f"  DCA grid kept until TP1 fills")

    if not args.dry_run:
        cancel_legacy_exit_algos(symbol, is_long, api, sec, args.recv_window)
        cancel_all_staged_algos(symbol, api, sec, args.recv_window)

    state: dict[str, Any] = {
        "takeover_done": True,
        "is_long": is_long,
        "entry_anchor": entry,
        "entry": entry,
        "tp1_profit_pct": args.tp1_profit_pct,
        "tp1_price": tp1_target,
        "trail_wall_price": wall["activation"],
        "armed_qty": float(total_d),
        "algo_ids": {},
    }
    state = place_tp1_order(
        symbol, is_long, qty, state, args, hedge, api, sec, filt,
        reason=f"{YELLOW}Placing TP1:{RESET}",
    )
    if not args.dry_run and state.get("phase") == PHASE_TP1:
        import telegram_notify as telegram
        direction = "LONG" if is_long else "SHORT"
        lev = get_symbol_leverage(symbol, api, sec, args.recv_window)
        _, pnl = position_pnl(symbol, is_long, hedge, api, sec, args.recv_window)
        telegram.notify_staged_armed(
            symbol.upper(), direction, float(total_d), entry, tp1_target, args.tp1_profit_pct,
            tp1_qty=float(state.get("tp1_qty", 0) or 0),
            leverage=lev,
            pnl_usdt=pnl,
        )
    return state


def execute_tp1_at_profit(
    symbol: str,
    is_long: bool,
    qty: float,
    state: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> dict[str, Any]:
    """Legacy: migrate waiting_profit state → place TP1 on exchange."""
    print(f"\n{YELLOW}Migrating to exchange TP1 (was waiting for poll){RESET}")
    return place_tp1_order(
        symbol, is_long, qty, state, args, hedge, api, sec, filt,
    )


def _tp1_filled(state: dict[str, Any], qty: float, step: Decimal) -> bool:
    initial = float(state.get("armed_qty", state.get("pre_armed_qty", 0)) or 0)
    if initial <= 0:
        return False
    tp1 = float(state.get("tp1_qty", 0) or 0)
    remain_expected = float(state.get("remain_qty", initial - tp1) or 0)
    tol = float(step) * 1.5
    if remain_expected > 0 and abs(qty - remain_expected) <= tol and qty < initial - tol:
        return True
    return qty <= remain_expected + tol and qty < initial - tol


def _transition_to_partial(
    symbol: str,
    is_long: bool,
    qty: float,
    state: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> dict[str, Any]:
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    entry_anchor = float(state.get("entry_anchor", state.get("entry", 0)) or 0)

    cancel_all_staged_algos(symbol, api, sec, args.recv_window)

    qty_d = _round_to(qty, step, ROUND_DOWN)
    qty_str = f"{qty_d:.{qty_dp}f}"
    be = entry_stop_price(entry_anchor, is_long, tick)
    be_str = f"{be:.{price_dp}f}"
    close_side = "SELL" if is_long else "BUY"

    wall = trail_wall_plan(symbol, entry_anchor, is_long, args, tick)
    state["trail_wall_price"] = wall["activation"]

    print(f"{YELLOW}TP1 filled → cancel DCA · SL runner {qty_str} @ entry {be_str}{RESET}")

    if args.dry_run:
        state.update({"phase": PHASE_PARTIAL, "remain_qty": float(qty_d), "be_price": be, "algo_ids": {}})
        return state

    cancel_dca_grid_orders(symbol, api, sec, args.recv_window)
    mark = get_mark_price(symbol, api, sec, args.recv_window)
    if _stop_would_immediately_trigger(is_long, be, mark, tick):
        print(f"{YELLOW}Entry SL would trigger immediately (mark {price_fmt(mark)}) — "
              f"closing runner {close_side} MARKET {qty_str}{RESET}")
        _market_reduce_qty(symbol, is_long, qty_d, hedge, filt, api, sec, args.recv_window)
        _, runner_qty, _ = _detect_open_side(symbol, hedge, api, sec, args.recv_window, is_long)
        if runner_qty <= 0:
            cancel_all_staged_algos(symbol, api, sec, args.recv_window)
            state.update({"phase": PHASE_IDLE, "remain_qty": 0.0, "algo_ids": {}})
            return state
        if _wall_retested(is_long, mark, wall["activation"], tick):
            return _transition_to_trail(symbol, is_long, runner_qty, state, args, hedge, api, sec, filt)
        runner_d = _round_to(runner_qty, step, ROUND_DOWN)
        state.update({
            "phase": PHASE_PARTIAL,
            "remain_qty": float(runner_d),
            "be_price": be,
            "algo_ids": {},
        })
        return state

    try:
        be_resp = place_algo_order(
            symbol, is_long, "STOP_MARKET", qty_str, be_str,
            hedge, api, sec, args.recv_window, client_tag="BE",
        )
    except RuntimeError as exc:
        if not _is_immediate_trigger_error(exc):
            raise
        print(f"{YELLOW}BE conditional rejected (-2021) — closing runner at market{RESET}")
        _market_reduce_qty(symbol, is_long, qty_d, hedge, filt, api, sec, args.recv_window)
        _, runner_qty, _ = _detect_open_side(symbol, hedge, api, sec, args.recv_window, is_long)
        if runner_qty <= 0:
            cancel_all_staged_algos(symbol, api, sec, args.recv_window)
            state.update({"phase": PHASE_IDLE, "remain_qty": 0.0, "algo_ids": {}})
            return state
        if _wall_retested(is_long, mark, wall["activation"], tick):
            return _transition_to_trail(symbol, is_long, runner_qty, state, args, hedge, api, sec, filt)
        runner_d = _round_to(runner_qty, step, ROUND_DOWN)
        state.update({
            "phase": PHASE_PARTIAL,
            "remain_qty": float(runner_d),
            "be_price": be,
            "algo_ids": {},
        })
        return state

    print(f"{GREEN}✓ SL @ entry {close_side} {qty_str} @ {be_str} algoId={be_resp.get('algoId')}{RESET}")
    import telegram_notify as telegram
    direction = "LONG" if is_long else "SHORT"
    tp1_qty = float(state.get("tp1_qty", 0) or 0)
    initial = float(state.get("pre_armed_qty", state.get("armed_qty", 0)) or 0)
    closed_pct = (tp1_qty / initial * 100) if initial > 0 else args.tp_partial_pct
    runner_pct = (float(qty_d) / initial * 100) if initial > 0 else (100.0 - closed_pct)
    lev = get_symbol_leverage(symbol, api, sec, args.recv_window)
    tp1_price = float(state.get("tp1_price", entry_anchor) or entry_anchor)
    _, pnl = position_pnl(symbol, is_long, hedge, api, sec, args.recv_window)
    telegram.notify_tp1_filled(
        symbol.upper(), direction, tp1_qty, float(qty_d), entry_anchor,
        tp1_price=tp1_price, leverage=lev, pnl_usdt=pnl,
    )
    telegram.notify_profit_lock_sl(
        symbol.upper(), direction, float(qty_d), entry_anchor, float(be),
        closed_pct=closed_pct,
        runner_pct=runner_pct,
        trigger=f"+{args.tp1_profit_pct:g}%",
        closed_qty=tp1_qty,
        leverage=lev,
        pnl_usdt=pnl,
    )
    state.update({
        "phase": PHASE_PARTIAL,
        "remain_qty": float(qty_d),
        "be_price": be,
        "algo_ids": {"be": be_resp.get("algoId")},
    })
    return state


def _transition_to_trail(
    symbol: str,
    is_long: bool,
    qty: float,
    state: dict[str, Any],
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> dict[str, Any]:
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    tp1_price = float(state.get("trail_wall_price", 0) or state.get("tp1_price", 0) or 0)
    act_str = f"{tp1_price:.{price_dp}f}"

    cancel_all_staged_algos(symbol, api, sec, args.recv_window)

    qty_d = _round_to(qty, step, ROUND_DOWN)
    qty_str = f"{qty_d:.{qty_dp}f}"
    close_side = "SELL" if is_long else "BUY"

    print(f"{CYAN}Opposite wall @ {act_str} → trailing on runner {qty_str}{RESET}")

    if args.dry_run:
        state.update({"phase": PHASE_TRAIL, "remain_qty": float(qty_d), "algo_ids": {}})
        return state

    tr_resp = place_algo_order(
        symbol, is_long, "TRAILING_STOP_MARKET", qty_str, act_str,
        hedge, api, sec, args.recv_window,
        client_tag="TR", callback=args.tp_callback, activate_str=act_str,
    )
    print(f"{GREEN}✓ Trail {close_side} {qty_str} activate @ {act_str} "
          f"callback {args.tp_callback:g}% algoId={tr_resp.get('algoId')}{RESET}")
    import telegram_notify as telegram
    direction = "LONG" if is_long else "SHORT"
    lev = get_symbol_leverage(symbol, api, sec, args.recv_window)
    entry_anchor = float(state.get("entry_anchor", state.get("entry", 0)) or 0)
    _, pnl = position_pnl(symbol, is_long, hedge, api, sec, args.recv_window)
    telegram.notify_trail_started(
        symbol.upper(), direction, float(qty_d), tp1_price, args.tp_callback,
        entry=entry_anchor, leverage=lev, pnl_usdt=pnl,
    )
    state.update({
        "phase": PHASE_TRAIL,
        "remain_qty": float(qty_d),
        "algo_ids": {"trail": tr_resp.get("algoId")},
    })
    return state


def _wall_retested(is_long: bool, mark: float, wall_price: float, tick: Decimal) -> bool:
    """True when price reaches the opposite OB wall (trailing activation level)."""
    tol = float(tick) * 0.5
    if is_long:
        return mark >= wall_price - tol
    return mark <= wall_price + tol


def _count_dca_orders(symbol: str, api: str, sec: str, recv: int) -> int:
    try:
        oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv) or []
    except Exception:
        return 0
    return sum(1 for o in oo if _order_client_id(o).startswith("obdca"))


def _entry_order_open(symbol: str, api: str, sec: str, recv: int) -> bool:
    """True if the DCA bot's tagged entry limit is still resting (not filled yet)."""
    sym = symbol.upper()
    want = f"obdcaE{sym}"
    try:
        oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, recv) or []
    except Exception:
        return False
    return any(_order_client_id(o) == want for o in oo)


def _print_flat_status(sym: str, args: argparse.Namespace, api: str, sec: str) -> None:
    n_dca = _count_dca_orders(sym, api, sec, args.recv_window)
    dry = "DRY-RUN — " if args.dry_run else ""
    print(f"{DIM}{dry}{sym} flat — no position to manage.{RESET}")
    print(f"  DCA limits (obdca*): {n_dca}")
    if n_dca:
        print(f"  {DIM}Waiting for entry fill → then watch +{args.tp1_profit_pct:g}% for TP1.{RESET}")
    else:
        print(f"  {YELLOW}Start the DCA bot first, e.g.:{RESET}")
        print(f"    python3 orderbook_dca_grid.py {sym} --supervise --no-tp --direction short")
    if args.once and not args.dry_run:
        print(f"  {DIM}Use the default loop to watch automatically:{RESET}")
        print(f"    python3 orderbook_staged_exit.py {sym}")


def manage_staged_once(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    prefer_is_long: bool | None = None,
) -> None:
    sym = symbol.upper()
    step = filt["step_size"]
    side_is_long, qty, entry = _detect_open_side(sym, hedge, api, sec, args.recv_window, prefer_is_long)

    if side_is_long is None or qty <= 0:
        state = load_state(sym)
        had_staged = state.get("phase", PHASE_IDLE) != PHASE_IDLE
        if had_staged:
            print(f"{DIM}{sym} flat — clearing staged state.{RESET}")
            if not args.dry_run:
                cancel_all_staged_algos(sym, api, sec, args.recv_window)
                cancel_dca_grid_orders(sym, api, sec, args.recv_window)
            clear_state(sym)
        elif not args.dry_run:
            n_dca = _count_dca_orders(sym, api, sec, args.recv_window)
            if n_dca > 0 and not _entry_order_open(sym, api, sec, args.recv_window):
                print(f"{YELLOW}{sym} flat — cancelling {n_dca} leftover DCA limit(s).{RESET}")
                cancel_dca_grid_orders(sym, api, sec, args.recv_window)
        if args.once or args.dry_run:
            _print_flat_status(sym, args, api, sec)
        return

    state = load_state(sym)
    phase = state.get("phase", PHASE_IDLE)
    if phase == "staged_full":
        phase = PHASE_WAITING
        state["phase"] = PHASE_WAITING

    if not state.get("takeover_done") or phase == PHASE_IDLE:
        if _entry_order_open(sym, api, sec, args.recv_window) and args.once:
            print(
                f"{DIM}{sym} entry limit still on book — arming TP1 on open position "
                f"{qty:g} @ {price_fmt(entry)}{RESET}",
            )
        state = arm_staged_exit(sym, side_is_long, qty, entry, args, hedge, api, sec, filt)
        save_state(sym, state)
        return

    if state.get("is_long") != side_is_long:
        print(f"{YELLOW}Side changed — re-arming staged exit.{RESET}")
        clear_state(sym)
        state = arm_staged_exit(sym, side_is_long, qty, entry, args, hedge, api, sec, filt)
        save_state(sym, state)
        return

    entry_anchor = float(state.get("entry_anchor", state.get("entry", entry)) or entry)
    mark = get_mark_price(sym, api, sec, args.recv_window)
    pnow = profit_pct(entry_anchor, mark, side_is_long)

    if phase == PHASE_WAITING:
        state = execute_tp1_at_profit(
            sym, side_is_long, qty, state, args, hedge, api, sec, filt,
        )
        save_state(sym, state)
        return

    if phase == PHASE_TP1:
        if not args.dry_run:
            reconcile_staged_algos(sym, phase, api, sec, args.recv_window)
        tp1_algo = find_our_algo(sym, "TP1", api, sec, args.recv_window)
        armed = float(state.get("armed_qty", 0) or 0)
        remain = float(state.get("remain_qty", 0) or 0)
        pre = float(state.get("pre_armed_qty", 0) or 0)
        partial_pct = args.tp_partial_pct / 100.0
        if pre <= 0 and partial_pct < 1.0 and qty > 0:
            pre = qty / (1.0 - partial_pct)
        if tp1_algo and pre > qty + float(step) * 2:
            try:
                aq = float(tp1_algo.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                aq = 0.0
            if abs(aq - qty) < float(step):
                cancel_our_algos(sym, "TP1", api, sec, args.recv_window)
                state["pre_armed_qty"] = pre
                state["remain_qty"] = qty
                state = _transition_to_partial(
                    sym, side_is_long, qty, state, args, hedge, api, sec, filt,
                )
                save_state(sym, state)
                return
        if not tp1_algo and armed > qty + float(step) and (
            remain <= 0 or abs(qty - remain) <= float(step) * 1.5
        ):
            state = _transition_to_partial(
                sym, side_is_long, qty, state, args, hedge, api, sec, filt,
            )
            save_state(sym, state)
            return
        if not tp1_algo and pre > 0 and qty < pre - float(step) / 2:
            state = _transition_to_partial(
                sym, side_is_long, qty, state, args, hedge, api, sec, filt,
            )
            save_state(sym, state)
            return
        if _tp1_filled(state, qty, step):
            state = _transition_to_partial(
                sym, side_is_long, qty, state, args, hedge, api, sec, filt,
            )
            save_state(sym, state)
            return
        if _tp1_qty_needs_sync(state, qty, step):
            print(f"{YELLOW}Position size changed {state.get('armed_qty', 0):g} → {qty:g} — updating TP1{RESET}")
            state = place_tp1_order(
                sym, side_is_long, qty, state, args, hedge, api, sec, filt,
            )
            save_state(sym, state)
            return
        elif args.once:
            side = "LONG" if side_is_long else "SHORT"
            print(f"{DIM}{sym} {side} {qty:g} @ {price_fmt(entry_anchor)} — TP1 @ {price_fmt(float(state.get('tp1_price', 0) or 0))} "
                  f"· profit now {pnow:+.2f}% (DCA kept){RESET}")
        return

    wall_price = float(state.get("trail_wall_price", 0) or 0)
    if phase == PHASE_PARTIAL:
        if not args.dry_run:
            reconcile_staged_algos(sym, phase, api, sec, args.recv_window)
        wall = trail_wall_plan(sym, entry_anchor, side_is_long, args, filt["tick_size"])
        wall_price = wall["activation"]
        state["trail_wall_price"] = wall_price
        if _wall_retested(side_is_long, mark, wall_price, filt["tick_size"]):
            state = _transition_to_trail(sym, side_is_long, qty, state, args, hedge, api, sec, filt)
        elif args.once:
            print(f"{DIM}{sym} SL @ entry {price_fmt(float(state.get('be_price', entry_anchor) or entry_anchor))} "
                  f"· waiting trail wall {price_fmt(wall_price)} (mark {price_fmt(mark)}){RESET}")
        save_state(sym, state)
        return

    if phase == PHASE_TRAIL:
        if not args.dry_run:
            reconcile_staged_algos(sym, phase, api, sec, args.recv_window)
        existing = find_our_algo(sym, "TR", api, sec, args.recv_window)
        qty_d = _round_to(qty, step, ROUND_DOWN)
        qty_dp = _dec_places(step)
        qty_str = f"{qty_d:.{qty_dp}f}"
        if existing:
            try:
                eq = abs(float(existing.get("quantity", 0) or 0) - float(qty_str)) < float(step) / 2
            except (TypeError, ValueError):
                eq = False
            if eq:
                if args.once:
                    print(f"{DIM}{sym} phase staged_trail — trailing {qty_str} in sync.{RESET}")
                return
        if not args.dry_run:
            cancel_all_staged_algos(sym, api, sec, args.recv_window)
            state = _transition_to_trail(sym, side_is_long, qty, state, args, hedge, api, sec, filt)
            save_state(sym, state)
        elif args.once:
            print(f"{DIM}{sym} phase staged_trail — would resync trailing qty → {qty_str}.{RESET}")


def supervise_loop(args: argparse.Namespace) -> None:
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot supervise staged exit.{RESET}")
        return
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    hedge = _resolve_hedge(args, api, sec)
    prefer = None
    if args.direction in ("long", "short"):
        prefer = args.direction == "long"

    print(f"\n{BOLD}{CYAN}Staged exit supervisor on {args.symbol.upper()} "
          f"(TP1 {args.tp_partial_pct:g}% @ +{args.tp1_profit_pct:g}% profit, poll {args.poll_sec:g}s). "
          f"Ctrl+C to stop.{RESET}")
    flat_log: str | None = None
    try:
        while True:
            try:
                side_is_long, qty, _ = _detect_open_side(
                    args.symbol, hedge, api, sec, args.recv_window, prefer,
                )
                if side_is_long is None or qty <= 0:
                    n_dca = _count_dca_orders(args.symbol, api, sec, args.recv_window)
                    state_key = f"flat:{n_dca}"
                    if state_key != flat_log:
                        flat_log = state_key
                        if n_dca:
                            print(f"{DIM}{args.symbol.upper()} flat — {n_dca} DCA limit(s) waiting to fill "
                                  f"→ SL + TP1 will arm on entry.{RESET}")
                        else:
                            print(f"{YELLOW}{args.symbol.upper()} flat and no DCA grid.{RESET}")
                            print(f"  {BOLD}Open a second terminal and run:{RESET}")
                            print(f"    python3 orderbook_dca_grid.py {args.symbol.upper()} "
                                  f"--supervise --no-tp --direction short")
                            print(f"  {DIM}This script only manages exits; it does not place the grid.{RESET}")
                    manage_staged_once(
                        args.symbol, args, hedge, api, sec, filt, prefer_is_long=prefer,
                    )
                else:
                    flat_log = None
                    manage_staged_once(
                        args.symbol, args, hedge, api, sec, filt, prefer_is_long=prefer,
                    )
            except Exception as exc:
                print(f"{RED}Supervisor pass error: {exc}{RESET}")
            time.sleep(args.poll_sec)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped staged exit supervisor (orders left in place).")


def audit_symbol(args: argparse.Namespace) -> None:
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys.{RESET}")
        return
    sym = args.symbol.upper()
    hedge = _resolve_hedge(args, api, sec)
    prefer = args.direction == "long" if args.direction in ("long", "short") else None
    side_is_long, qty, entry = _detect_open_side(sym, hedge, api, sec, args.recv_window, prefer)
    state = load_state(sym)
    phase = state.get("phase", PHASE_IDLE)

    if getattr(args, "cleanup", False) and not args.dry_run:
        n = cancel_all_staged_algos(sym, api, sec, args.recv_window)
        print(f"{GREEN}Cleanup: cancelled {n} open obstage* algo(s) on {sym}.{RESET}")
        print(f"{DIM}If the chart still shows Stop/TP lines, refresh the page — "
              f"FINISHED algos often leave ghost overlays.{RESET}")

    algos = list_open_algo_orders(sym, api, sec, args.recv_window)
    ours = [o for o in algos if _algo_client_id(o).startswith(ALGO_PREFIX)]
    legacy = [o for o in algos if not _algo_client_id(o).startswith(ALGO_PREFIX)]
    allowed = ALLOWED_ALGOS_BY_PHASE.get(phase, set())
    stray = [o for o in ours if _staged_tag_from_cid(_algo_client_id(o), sym) not in allowed]

    try:
        oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, args.recv_window) or []
    except Exception:
        oo = []
    dca = [o for o in oo if _order_client_id(o).startswith("obdca")]

    print(f"\n{BOLD}{CYAN}=== {sym} staged exit audit ==={RESET}")
    print(f"  State phase: {phase}  takeover={state.get('takeover_done', False)}")
    if allowed:
        print(f"  Allowed algos this phase: {', '.join(sorted(allowed))}")
    if stray:
        print(f"  {YELLOW}Stray algos (wrong phase): {len(stray)}{RESET}")
    if side_is_long is not None:
        mark = get_mark_price(sym, api, sec, args.recv_window)
        entry_a = float(state.get("entry_anchor", entry) or entry)
        pnow = profit_pct(entry_a, mark, side_is_long)
        print(f"  Position: {'LONG' if side_is_long else 'SHORT'} {qty:g} @ {entry:g}  mark={price_fmt(mark)}  "
              f"PnL vs anchor {pnow:+.2f}%")
        if state.get("entry_anchor"):
            print(f"  Entry anchor: {price_fmt(entry_a)}  TP1 @ +{state.get('tp1_profit_pct', args.tp1_profit_pct):g}% "
                  f"→ {price_fmt(float(state.get('tp1_price', 0) or 0))}")
        if state.get("trail_wall_price"):
            print(f"  Trail wall: {price_fmt(float(state['trail_wall_price']))}")
        if state.get("be_price"):
            print(f"  SL @ entry: {price_fmt(float(state['be_price']))}")
    else:
        print(f"  Position: flat")
    print(f"  DCA limits (obdca*): {len(dca)}  ·  our algos (open): {len(ours)}  ·  legacy algos: {len(legacy)}")
    for o in ours:
        otype = o.get("orderType") or o.get("type")
        tag = _staged_tag_from_cid(_algo_client_id(o), sym)
        flag = f" {YELLOW}[stray for phase {phase}]{RESET}" if tag not in allowed else ""
        print(f"    {DIM}{_algo_client_id(o)} {otype} qty={o.get('quantity')} "
              f"trigger={o.get('triggerPrice')}{RESET}{flag}")
    if not ours and phase in ALLOWED_ALGOS_BY_PHASE:
        print(f"  {DIM}No open obstage* algos — chart Stop/TP lines may be ghosts from FINISHED orders.{RESET}")
        print(f"  {DIM}Refresh the Binance page (F5) to clear them.{RESET}")


def parse_args() -> argparse.Namespace:
    env_file = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--env-file" and i + 1 < len(argv):
            env_file = argv[i + 1]
            break
    load_env_file(env_file)

    p = argparse.ArgumentParser(
        description="Staged futures exit addon (70% @ profit target + SL @ entry + trailing wall). "
                    "Default: automatic supervise loop.",
    )
    p.add_argument("symbol", nargs="?", help="Symbol, e.g. LINKUSDT")
    p.add_argument("--supervise", action="store_true", help="Autonomous loop (default if no --once/--audit)")
    p.add_argument("--once", action="store_true", help="Single sync pass then exit")
    p.add_argument("--audit", action="store_true", help="Read-only status")
    p.add_argument("--cleanup", action="store_true",
                   help="With --audit: cancel all open obstage* algos on the symbol")
    p.add_argument("--dry-run", action="store_true", help="Preview only — no orders")
    p.add_argument("--direction", choices=["long", "short"], default=None,
                   help="Pin position side detection (default: auto-detect)")
    p.add_argument("--tp1-profit-pct", type=float, default=_env_float("TP1_PROFIT_PCT", 0.3),
                   help="Take first partial when profit reaches this %%. Env: TP1_PROFIT_PCT")
    p.add_argument("--tp-partial-pct", type=float, default=_env_float("TP_PARTIAL_PCT", 70.0),
                   help="First take-profit size %%. Env: TP_PARTIAL_PCT")
    p.add_argument("--tp-callback", type=float, default=_env_float("TP_CALLBACK", 0.2))
    p.add_argument("--tp-fee-buffer", type=float, default=_env_float("TP_FEE_BUFFER", 0.12))
    p.add_argument("--tp-wall-min-mult", type=float, default=3.0)
    p.add_argument("--tp-wall-pick", choices=["nearest", "strongest"], default="nearest")
    p.add_argument("--sl-pct", type=float, default=_env_float("SL_PCT", 2.0),
                   help="Stop loss %% adverse from entry. Env: SL_PCT")
    p.add_argument("--sl-wall", action="store_true", help="Use adverse OB wall for SL (within sl-pct band)")
    p.add_argument("--cancel-dca", action="store_true",
                   help="On takeover: cancel obdca* grid limits (default: keep DCA running)")
    p.add_argument("--limit", type=int, default=100, help="Order book depth limit")
    p.add_argument("--poll-sec", type=float, default=_env_float("STAGED_POLL_SEC", 5.0))
    p.add_argument("--position-mode", choices=["auto", "hedge", "oneway"], default="auto")
    p.add_argument("--recv-window", type=int, default=_env_int("RECV_WINDOW", 15000),
                   help="Binance recvWindow ms. Env: RECV_WINDOW")
    p.add_argument("--env-file", default=None)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.symbol:
        print(f"{RED}Symbol required (e.g. LINKUSDT).{RESET}")
        sys.exit(1)

    if args.audit:
        audit_symbol(args)
        return

    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — set BINANCE_API_KEY / BINANCE_SECRET_KEY in .env{RESET}")
        sys.exit(1)

    if args.once:
        try:
            filt = load_symbol_filters(args.symbol)
        except Exception as exc:
            print(f"{RED}Could not load symbol filters: {exc}{RESET}")
            sys.exit(1)
        hedge = _resolve_hedge(args, api, sec)
        prefer = args.direction == "long" if args.direction in ("long", "short") else None
        manage_staged_once(
            args.symbol, args, hedge, api, sec, filt, prefer_is_long=prefer,
        )
        return

    # Default: automatic supervise loop (TP placed on position detect)
    supervise_loop(args)


if __name__ == "__main__":
    main()
