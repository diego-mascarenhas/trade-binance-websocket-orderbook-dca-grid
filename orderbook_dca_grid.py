#!/usr/bin/env python3
"""DCA grid anchored to REAL order book levels (not a geometric derivation).

Instead of deriving DCA prices from a step formula, this reads the live order
book and places each DCA on an actual wall going *backwards* from entry:
  - LONG  -> significant BID walls below the entry price
  - SHORT -> significant ASK walls above the entry price

Sizes still compensate so the take-profit keeps the SAME relationship to the
position (TP is a fixed % above/below the running average price).

Self-contained: Python standard library only. No API keys.

Usage:
    python orderbook_dca_grid.py MORPHOUSDT                 # live book + live entry
    python orderbook_dca_grid.py MORPHOUSDT --price 2.0816  # fixed entry
    python orderbook_dca_grid.py ETHUSDT --direction short
    python orderbook_dca_grid.py MORPHOUSDT --size-mode wall --so-count 8
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_UP, Decimal

FAPI_BASE = os.getenv("FAPI_BASE", "https://fapi.binance.com").rstrip("/")

GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


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


def decide_direction(
    bids: list[list[float]], asks: list[list[float]], mid: float, range_pct: float
) -> dict:
    """Decide long/short from bid vs ask resting liquidity near the mid.

    More bid (support) than ask (resistance) within the band -> long, else short.
    """
    lo = mid * (1 - range_pct / 100)
    hi = mid * (1 + range_pct / 100)
    bid_vol = sum(q for p, q in bids if p >= lo)
    ask_vol = sum(q for p, q in asks if p <= hi)
    total = bid_vol + ask_vol
    imb = (bid_vol / total) if total else 0.5
    direction = "long" if bid_vol >= ask_vol else "short"
    return {"direction": direction, "bid_vol": bid_vol, "ask_vol": ask_vol, "imbalance": imb}


def select_walls(
    levels: list[list[float]],
    entry: float,
    is_long: bool,
    count: int,
    min_gap_pct: float,
    min_dist_pct: float,
    max_range_pct: float,
) -> list[tuple[float, float, float]]:
    """Pick the most significant walls (by resting size) going backwards from entry.

    Greedy by liquidity, enforcing a minimum price gap so picks are spread out.
    Returns [(price, wall_qty, dist_pct), ...] sorted by distance from entry.
    """
    cands: list[tuple[float, float, float]] = []
    for price, qty in levels:
        if is_long and price >= entry:
            continue
        if not is_long and price <= entry:
            continue
        dist = abs(price - entry) / entry * 100
        if dist < min_dist_pct:
            continue
        if max_range_pct > 0 and dist > max_range_pct:
            continue
        cands.append((price, qty, dist))

    cands.sort(key=lambda x: x[1], reverse=True)  # biggest walls first
    picked: list[tuple[float, float, float]] = []
    for price, qty, dist in cands:
        if all(abs(price - p) / entry * 100 >= min_gap_pct for p, _, _ in picked):
            picked.append((price, qty, dist))
        if len(picked) >= count:
            break

    picked.sort(key=lambda x: x[2])  # nearest to entry = DCA #1
    return picked


def build_grid(
    entry: float,
    is_long: bool,
    walls: list[tuple[float, float, float]],
    base_size: float,
    tp_pct: float,
    size_mode: str,
    comp_factor: float,
    so_size: float,
    volume_scale: float,
) -> list[dict]:
    orders: list[dict] = [
        {
            "name": "Base Order",
            "price": entry,
            "wall_qty": None,
            "size_usdt": base_size,
            "qty": base_size / entry,
        }
    ]

    prev_dist = 0.0
    for i, (price, wall_qty, dist) in enumerate(walls, start=1):
        if size_mode == "comp":
            # Distance compensation: farther wall -> bigger add (keeps TP relationship).
            band = max(dist - prev_dist, 0.0)
            size = base_size * band * comp_factor
        elif size_mode == "wall":
            size = None  # filled in after we know total wall liquidity
        elif size_mode == "scale":
            size = so_size * (volume_scale ** (i - 1))
        else:  # flat
            size = so_size
        orders.append(
            {
                "name": f"DCA #{i}",
                "price": price,
                "wall_qty": wall_qty,
                "size_usdt": size,
                "dist": dist,
            }
        )
        prev_dist = dist

    if size_mode == "wall":
        total_wall = sum(w[1] for w in walls) or 1.0
        # Distribute a budget proportional to each wall's liquidity.
        budget = base_size * len(walls)  # default budget = base_size per DCA on average
        for o in orders[1:]:
            o["size_usdt"] = budget * (o["wall_qty"] / total_wall)

    for o in orders[1:]:
        o["qty"] = o["size_usdt"] / o["price"]

    cum_qty = 0.0
    cum_usdt = 0.0
    for o in orders:
        cum_qty += o["qty"]
        cum_usdt += o["size_usdt"]
        avg = cum_usdt / cum_qty
        o["cum_usdt"] = cum_usdt
        o["cum_qty"] = cum_qty
        o["avg"] = avg
        o["tp"] = avg * (1 + tp_pct / 100) if is_long else avg * (1 - tp_pct / 100)
        o["delta_pct"] = (o["price"] / entry - 1) * 100
    return orders


def render(symbol: str, args: argparse.Namespace, orders: list[dict], entry: float, found: int) -> str:
    is_long = args.direction == "long"
    dir_color = GREEN if is_long else RED
    tp_sign = "+" if is_long else "-"
    side = "BID walls below" if is_long else "ASK walls above"

    lines: list[str] = []
    lines.append(
        f"{BOLD}{CYAN}OB DCA Grid · {symbol} · "
        f"{dir_color}{args.direction.upper()} {args.leverage:g}x{CYAN}{RESET}"
    )
    lines.append(
        f"{DIM}entry {price_fmt(entry)}  ·  {found} DCA on {side}  ·  "
        f"size-mode {args.size_mode}  ·  TP {tp_sign}{args.tp:g}% from avg  ·  "
        f"depth limit {args.limit}, min-gap {args.min_gap:g}%{RESET}"
    )
    lines.append("")

    header = (
        f"{'ORDER':<10} {'QTY':>12} {'PRICE':>13} {'Δ ENTRY':>9} "
        f"{'WALL':>12} {'SIZE USDT':>11} {'POS USDT':>11} {'AVG':>13} {'TP PRICE':>13}"
    )
    lines.append(f"{DIM}{header}{RESET}")
    lines.append(f"{DIM}{'─' * len(header)}{RESET}")

    for o in orders:
        is_base = o["name"] == "Base Order"
        row_color = "" if is_base else (GREEN if is_long else RED)
        wall = "—" if o["wall_qty"] is None else qty_fmt(o["wall_qty"])
        lines.append(
            f"{row_color}{o['name']:<10} {qty_fmt(o['qty']):>12} "
            f"{price_fmt(o['price']):>13} {o['delta_pct']:>+8.2f}% "
            f"{wall:>12} {o['size_usdt']:>11,.2f} {o['cum_usdt']:>11,.2f} "
            f"{price_fmt(o['avg']):>13} {price_fmt(o['tp']):>13}{RESET}"
        )

    last = orders[-1]
    margin = last["cum_usdt"] / args.leverage
    lines.append("")
    lines.append(
        f"{BOLD}Full grid{RESET}  qty {qty_fmt(last['cum_qty'])}  ·  "
        f"notional {last['cum_usdt']:,.2f} USDT  ·  margin@{args.leverage:g}x {margin:,.2f} USDT"
    )
    lines.append(
        f"{BOLD}Full-fill avg{RESET} {price_fmt(last['avg'])}  ·  "
        f"{BOLD}Full-fill TP{RESET} {price_fmt(last['tp'])} ({tp_sign}{args.tp:g}%)"
    )
    lines.append(
        f"{DIM}Each DCA sits on a real order-book wall (WALL column = resting size there). "
        f"TP stays {tp_sign}{args.tp:g}% from the running average on every fill.{RESET}"
    )
    if found < args.so_count:
        lines.append(
            f"{RED}Only {found}/{args.so_count} qualifying walls found in the fetched depth. "
            f"Try a higher --limit or a smaller --min-gap / larger --max-range.{RESET}"
        )
    return "\n".join(lines)


# --- Execution (Binance Futures LIMIT orders) ----------------------------

def load_keys(env_file: str | None) -> tuple[str, str]:
    """Read API keys from environment, falling back to a .env file.

    Lookup order: environment vars → --env-file → ./.env (cwd) → .env next to
    this script.
    """
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    if api and sec:
        return api, sec
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        env_file,
        os.path.join(os.getcwd(), ".env"),
        os.path.join(here, ".env"),
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() == "BINANCE_API_KEY" and not api:
                    api = v
                elif k.strip() == "BINANCE_SECRET_KEY" and not sec:
                    sec = v
        if api and sec:
            break
    return api, sec


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


def prepare_orders(orders: list[dict], symbol: str, is_long: bool, filt: dict[str, Decimal]) -> list[dict]:
    """Round price/qty to exchange precision and enforce min qty / min notional."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    price_round = ROUND_DOWN if is_long else ROUND_UP  # entry limits do not cross away

    prepared: list[dict] = []
    for o in orders:
        price_d = _round_to(o["price"], tick, price_round)
        qty_d = _round_to(o["qty"], step, ROUND_DOWN)
        if qty_d < filt["min_qty"]:
            qty_d = filt["min_qty"]
        # Bump quantity up to satisfy min notional
        while qty_d * price_d < filt["min_notional"]:
            qty_d += step
        prepared.append(
            {
                "name": o["name"],
                "price": f"{price_d:.{price_dp}f}",
                "quantity": f"{qty_d:.{qty_dp}f}",
                "notional": float(price_d * qty_d),
            }
        )
    return prepared


def place_orders(
    symbol: str,
    is_long: bool,
    prepared: list[dict],
    args: argparse.Namespace,
) -> None:
    side = "BUY" if is_long else "SELL"
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys found (env or .env). Cannot execute.{RESET}")
        return

    # Position mode
    hedge = False
    if args.position_mode == "hedge":
        hedge = True
    elif args.position_mode == "oneway":
        hedge = False
    else:
        try:
            resp = _signed_request("GET", "/fapi/v1/positionSide/dual", {}, api, sec, args.recv_window)
            hedge = bool(resp.get("dualSidePosition"))
        except Exception as exc:
            print(f"{RED}Could not detect position mode ({exc}); assuming one-way.{RESET}")

    # Safety: refuse to place a new grid if there is already exposure on this symbol.
    if not args.force:
        ql, el = get_position(symbol, True, hedge, api, sec, args.recv_window)
        qs, es = get_position(symbol, False, hedge, api, sec, args.recv_window)
        try:
            existing_orders = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, args.recv_window)
        except Exception:
            existing_orders = []
        if ql > 0 or qs > 0 or existing_orders:
            detail = []
            if ql > 0:
                detail.append(f"LONG {ql} @ {el}")
            if qs > 0:
                detail.append(f"SHORT {qs} @ {es}")
            if existing_orders:
                detail.append(f"{len(existing_orders)} open order(s)")
            print(f"{RED}✗ {symbol.upper()} already has exposure ({', '.join(detail)}). "
                  f"Not placing a new grid. Use --force to override.{RESET}")
            return False

    # Leverage: explicit --set-leverage, else the symbol's max (unless --no-max-leverage).
    target_lev = None
    if args.set_leverage > 0:
        target_lev = int(args.set_leverage)
    elif not args.no_max_leverage:
        try:
            target_lev = get_max_leverage(symbol, api, sec, args.recv_window)
        except Exception as exc:
            print(f"{RED}Could not read max leverage: {exc}{RESET}")
    if target_lev:
        try:
            _signed_request(
                "POST", "/fapi/v1/leverage",
                {"symbol": symbol.upper(), "leverage": target_lev},
                api, sec, args.recv_window,
            )
            print(f"{DIM}Leverage set to {target_lev}x{RESET}")
        except Exception as exc:
            print(f"{RED}Set leverage failed: {exc}{RESET}")

    print(f"\n{BOLD}Placing {len(prepared)} {side} LIMIT orders on {symbol.upper()} "
          f"({'hedge' if hedge else 'one-way'} mode)...{RESET}")
    placed = 0
    for o in prepared:
        params = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "LIMIT",
            "timeInForce": args.tif,
            "quantity": o["quantity"],
            "price": o["price"],
        }
        if hedge:
            params["positionSide"] = "LONG" if is_long else "SHORT"
        try:
            resp = _signed_request("POST", "/fapi/v1/order", params, api, sec, args.recv_window)
            print(f"{GREEN}✓ {o['name']:<10} {side} {o['quantity']} @ {o['price']}  "
                  f"orderId={resp.get('orderId')}{RESET}")
            placed += 1
        except Exception as exc:
            print(f"{RED}✗ {o['name']:<10} {side} {o['quantity']} @ {o['price']}  → {exc}{RESET}")
    print(f"{BOLD}Placed {placed}/{len(prepared)} orders.{RESET}")
    return placed > 0


# --- Trailing TP on the OPPOSITE order book (profit-guaranteed) ----------

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
    """Pick the trailing-TP activation price from the opposite side of the book.

    The activation is snapped to a real wall (support for short, resistance for
    long) but clamped so the WORST-case trailing exit is still in profit:
        SHORT (BUY):  activation * (1 + callback%) <= avg * (1 - fee_buffer%)
        LONG  (SELL): activation * (1 - callback%) >= avg * (1 + fee_buffer%)
    """
    all_q = [q for _, q in bids] + [q for _, q in asks]
    med = statistics.median(all_q) if all_q else 0.0
    min_wall = med * wall_min_mult
    tickf = float(tick)

    if not is_long:  # SHORT position -> BUY trailing, activation BELOW avg
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
        profit_now = (avg - activation) / avg * 100
    else:  # LONG position -> SELL trailing, activation ABOVE avg
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
        profit_now = (activation - avg) / avg * 100

    return {
        "activation": activation,
        "wall_qty": wall[1] if wall else None,
        "on_wall": wall is not None,
        "worst_exit": worst_exit,
        "profit_worst_pct": profit_worst,
        "profit_activation_pct": profit_now,
        "min_wall": min_wall,
        "threshold": threshold,
    }


def get_wallet_balance(api: str, sec: str, recv: int, asset: str = "USDT") -> float:
    """Total wallet balance for an asset (USDT) on the futures account."""
    rows = _signed_request("GET", "/fapi/v2/balance", {}, api, sec, recv)
    for r in rows if isinstance(rows, list) else []:
        if r.get("asset") == asset:
            return float(r.get("balance", 0) or 0)
    return 0.0


def get_max_leverage(symbol: str, api: str, sec: str, recv: int) -> int:
    """Highest initial leverage allowed for the symbol (from its leverage bracket)."""
    br = _signed_request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol.upper()}, api, sec, recv)
    brs = br[0] if isinstance(br, list) else br
    return max(int(b["initialLeverage"]) for b in brs["brackets"])


def get_position(symbol: str, is_long: bool, hedge: bool, api: str, sec: str, recv: int) -> tuple[float, float]:
    """Return (abs_qty, entry_price) for the relevant position side (0,0 if none)."""
    rows = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, api, sec, recv)
    want_side = ("LONG" if is_long else "SHORT") if hedge else "BOTH"
    for r in rows:
        if str(r.get("positionSide", "BOTH")).upper() != want_side:
            continue
        amt = float(r.get("positionAmt", 0) or 0)
        if abs(amt) > 0:
            return abs(amt), float(r.get("entryPrice", 0) or 0)
    return 0.0, 0.0


def open_trailing_tp(symbol: str, is_long: bool, api: str, sec: str, recv: int) -> dict | None:
    """Find an existing TRAILING_STOP_MARKET algo order that closes this position."""
    try:
        resp = _signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv)
    except Exception:
        return None
    orders = resp if isinstance(resp, list) else resp.get("orders", resp.get("data", []))
    close_side = "SELL" if is_long else "BUY"
    for o in orders or []:
        otype = str(o.get("orderType") or o.get("type") or "").upper()
        if otype == "TRAILING_STOP_MARKET" and str(o.get("side", "")).upper() == close_side:
            return o
    return None


def cancel_foreign_sl(symbol: str, is_long: bool, api: str, sec: str, recv: int) -> int:
    """Cancel any STOP_MARKET/STOP order we didn't place (our script only uses
    TRAILING_STOP_MARKET). Keeps the DCA position free of external stop losses."""
    try:
        resp = _signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()}, api, sec, recv)
    except Exception:
        return 0
    orders = resp if isinstance(resp, list) else resp.get("orders", resp.get("data", []))
    close_side = "SELL" if is_long else "BUY"
    killed = 0
    for o in orders or []:
        otype = str(o.get("orderType") or o.get("type") or "").upper()
        if otype in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT") and \
           str(o.get("side", "")).upper() == close_side:
            try:
                _signed_request("DELETE", "/fapi/v1/algoOrder",
                                {"symbol": symbol.upper(), "algoId": o.get("algoId")}, api, sec, recv)
                trig = o.get("triggerPrice")
                print(f"{RED}✗ Removed foreign {otype} SL/TP (trigger {trig}) — DCA runs without it{RESET}")
                killed += 1
            except Exception as exc:
                print(f"{RED}Could not cancel foreign {otype}: {exc}{RESET}")
    return killed


def place_trailing_tp(
    symbol: str, is_long: bool, qty_str: str, activation_str: str,
    callback: float, hedge: bool, api: str, sec: str, recv: int,
) -> dict:
    side = "SELL" if is_long else "BUY"
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": qty_str,
        "callbackRate": callback,
        "activatePrice": activation_str,
        "workingType": "CONTRACT_PRICE",
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return _signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def _detect_open_side(symbol: str, hedge: bool, api: str, sec: str, recv: int,
                      prefer_is_long: bool | None = None) -> tuple[bool | None, float, float]:
    """Return (is_long, qty, entry) for whichever side has an open position."""
    order = (True, False) if prefer_is_long is None else (prefer_is_long, not prefer_is_long)
    for want_long in order:
        q, e = get_position(symbol, want_long, hedge, api, sec, recv)
        if q > 0:
            return want_long, q, e
    return None, 0.0, 0.0


def _manage_tp_once(symbol: str, side_is_long: bool, qty: float, entry: float,
                    args: argparse.Namespace, hedge: bool, api: str, sec: str,
                    filt: dict[str, Decimal]) -> None:
    """One TP-sync pass: clean foreign SL, and place/replace the trailing TP if needed."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    close_side = "SELL" if side_is_long else "BUY"

    if not args.keep_sl:
        cancel_foreign_sl(symbol, side_is_long, api, sec, args.recv_window)

    qty_d = _round_to(qty, step, ROUND_DOWN)
    qty_str = f"{qty_d:.{qty_dp}f}"

    existing = open_trailing_tp(symbol, side_is_long, api, sec, args.recv_window)
    qty_matches = False
    if existing:
        try:
            qty_matches = abs(float(existing.get("quantity", 0) or 0) - float(qty_str)) < float(step) / 2
        except (TypeError, ValueError):
            qty_matches = False

    # A TRAILING_STOP_MARKET already trails on Binance's side. Leave it alone —
    # only (re)place when the position size changed (a DCA filled) or no TP yet.
    if existing and qty_matches:
        return

    depth = fetch_depth(symbol, args.limit)
    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]
    latest = (bids[0][0] + asks[0][0]) / 2
    plan = choose_tp_activation(
        bids, asks, entry, latest, side_is_long, args.tp_callback,
        args.tp_fee_buffer, tick, args.tp_wall_min_mult, args.tp_wall_pick,
    )
    act_str = f"{plan['activation']:.{price_dp}f}"

    if existing:
        try:
            _signed_request("DELETE", "/fapi/v1/algoOrder",
                            {"symbol": symbol.upper(), "algoId": existing.get("algoId")},
                            api, sec, args.recv_window)
            print(f"{DIM}Position size changed → replacing TP with qty {qty_str}{RESET}")
        except Exception as exc:
            print(f"{RED}Cancel old TP failed: {exc}{RESET}")

    try:
        resp = place_trailing_tp(symbol, side_is_long, qty_str, act_str,
                                 args.tp_callback, hedge, api, sec, args.recv_window)
        wall_note = (f"wall {qty_fmt(plan['wall_qty'])}" if plan["on_wall"]
                     else "profit-floor (no wall deep enough)")
        print(f"{GREEN}✓ TP {close_side} {qty_str} activate @ {act_str} "
              f"({wall_note}) · worst-case profit {plan['profit_worst_pct']:+.2f}% "
              f"· algoId={resp.get('algoId')}{RESET}")
    except Exception as exc:
        print(f"{RED}✗ Place TP failed: {exc}{RESET}")


def manage_trailing_tp(symbol: str, is_long: bool | None, args: argparse.Namespace, hedge: bool,
                       api: str, sec: str, filt: dict[str, Decimal]) -> None:
    """Keep one reduce-only trailing TP synced to the live position, on the opposite OB.

    If `is_long` is None the open side is detected each pass (robust for a server
    that restarts while flat, regardless of the grid's direction).
    """
    print(f"\n{BOLD}{CYAN}Managing trailing TP on {symbol.upper()} "
          f"(callback {args.tp_callback:g}%, fee buffer {args.tp_fee_buffer:g}%, "
          f"poll {args.tp_poll_sec:g}s). Ctrl+C to stop.{RESET}")
    try:
        while True:
            side_is_long, qty, entry = _detect_open_side(symbol, hedge, api, sec, args.recv_window, is_long)
            if side_is_long is None:
                if not args.keep_sl:
                    cancel_foreign_sl(symbol, True, api, sec, args.recv_window)
                    cancel_foreign_sl(symbol, False, api, sec, args.recv_window)
                print(f"{DIM}No open position yet — waiting…{RESET}")
                time.sleep(args.tp_poll_sec)
                continue
            _manage_tp_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
            time.sleep(args.tp_poll_sec)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped managing TP (existing TP order left in place).")


def build_and_place_grid(args: argparse.Namespace, api: str, sec: str,
                         filt: dict[str, Decimal], verbose: bool = True) -> bool:
    """Compute (auto-direction, wallet%% size, max leverage) and place a fresh grid.
    Returns True if orders were placed. Used by --supervise for auto re-arming."""
    try:
        depth = fetch_depth(args.symbol, args.limit)
    except Exception as exc:
        print(f"{RED}Depth fetch failed: {exc}{RESET}")
        return False
    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]
    if not bids or not asks:
        return False
    mid = (bids[0][0] + asks[0][0]) / 2

    if args.direction == "auto":
        d = decide_direction(bids, asks, mid, args.auto_range)
        is_long = d["direction"] == "long"
        if verbose:
            print(f"{BOLD}{CYAN}Auto-direction: {d['direction'].upper()}{RESET} "
                  f"{DIM}(bid {d['bid_vol']:,.0f} vs ask {d['ask_vol']:,.0f}){RESET}")
    else:
        is_long = args.direction == "long"

    entry = args.price if args.price is not None else mid
    base_size = args.base_size
    if base_size <= 0:
        try:
            bal = get_wallet_balance(api, sec, args.recv_window)
            base_size = bal * args.wallet_pct / 100.0
        except Exception as exc:
            print(f"{RED}Wallet balance read failed: {exc}{RESET}")
            return False

    levels = bids if is_long else asks
    walls = select_walls(levels, entry, is_long, args.so_count, args.min_gap, args.min_dist, args.max_range)
    if not walls:
        print(f"{RED}No qualifying walls found (adjust --min-gap/--max-range/--limit).{RESET}")
        return False

    orders = build_grid(entry, is_long, walls, base_size, args.tp, args.size_mode,
                        args.comp_factor, args.so_size, args.volume_scale)
    prepared = prepare_orders(orders, args.symbol, is_long, filt)
    return place_orders(args.symbol, is_long, prepared, args)


def supervise_loop(args: argparse.Namespace) -> None:
    """Fully autonomous: re-place the grid whenever the symbol is flat, and keep
    the trailing TP synced while a position is open."""
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot supervise.{RESET}")
        return
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    hedge = _resolve_hedge(args, api, sec)
    print(f"\n{BOLD}{CYAN}Supervising {args.symbol.upper()} "
          f"(auto re-arm grid + trailing TP, poll {args.tp_poll_sec:g}s). Ctrl+C to stop.{RESET}")
    try:
        while True:
            try:
                side_is_long, qty, entry = _detect_open_side(args.symbol, hedge, api, sec, args.recv_window)
                if side_is_long is not None:
                    _manage_tp_once(args.symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
                else:
                    if not args.keep_sl:
                        cancel_foreign_sl(args.symbol, True, api, sec, args.recv_window)
                        cancel_foreign_sl(args.symbol, False, api, sec, args.recv_window)
                    oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": args.symbol.upper()}, api, sec, args.recv_window)
                    if oo:
                        print(f"{DIM}Flat · grid armed ({len(oo)} orders waiting to fill)…{RESET}")
                    else:
                        print(f"{BOLD}Flat and no orders → re-arming grid…{RESET}")
                        build_and_place_grid(args, api, sec, filt, verbose=True)
            except Exception as exc:
                print(f"{RED}Supervisor pass error: {exc}{RESET}")
            time.sleep(args.tp_poll_sec)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped supervising (open orders/TP left in place).")


def print_tp_plan(symbol: str, is_long: bool, args: argparse.Namespace,
                  bids: list[list[float]], asks: list[list[float]], avg: float, tick: Decimal) -> None:
    latest = (bids[0][0] + asks[0][0]) / 2
    plan = choose_tp_activation(
        bids, asks, avg, latest, is_long, args.tp_callback,
        args.tp_fee_buffer, tick, args.tp_wall_min_mult, args.tp_wall_pick,
    )
    close_side = "SELL" if is_long else "BUY"
    wall_note = (f"on {qty_fmt(plan['wall_qty'])} wall" if plan["on_wall"]
                 else "no wall deep enough → clamped to profit floor")
    ok = plan["profit_worst_pct"] > 0
    color = GREEN if ok else RED
    print(f"\n{BOLD}{CYAN}Trailing TP plan (opposite OB, for avg {price_fmt(avg)}):{RESET}")
    print(f"  close {close_side} TRAILING_STOP_MARKET · activate @ {price_fmt(plan['activation'])} "
          f"({wall_note})")
    print(f"  callback {args.tp_callback:g}%  →  worst-case exit {price_fmt(plan['worst_exit'])}")
    print(f"  {color}profit at activation {plan['profit_activation_pct']:+.2f}%  ·  "
          f"worst-case (after callback) {plan['profit_worst_pct']:+.2f}%  "
          f"{'✓ guaranteed in profit' if ok else '✗ NOT in profit'}{RESET}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DCA grid anchored to real order-book walls")
    p.add_argument("symbol", help="Symbol, e.g. MORPHOUSDT")
    p.add_argument("--price", type=float, default=None, help="Entry price (default: live mid)")
    p.add_argument("--direction", choices=["long", "short", "auto"], default="auto",
                   help="auto = decide from bid/ask imbalance in the book")
    p.add_argument("--auto-range", type=float, default=1.0, help="%% band around mid for auto-direction imbalance")
    p.add_argument("--so-count", type=int, default=8, help="Number of DCA orders (walls to place)")
    p.add_argument("--limit", type=int, default=1000, help="Order book depth to fetch (5..1000)")
    p.add_argument("--min-gap", type=float, default=0.8, help="Min %% spacing between chosen walls")
    p.add_argument("--min-dist", type=float, default=0.1, help="Min %% distance of first wall from entry")
    p.add_argument("--max-range", type=float, default=12.0, help="Only DCA walls within this %% of entry (0=off)")
    p.add_argument(
        "--size-mode",
        choices=["comp", "wall", "scale", "flat"],
        default="comp",
        help="comp=distance compensation, wall=∝ wall liquidity, scale=geometric, flat=equal",
    )
    p.add_argument("--base-size", type=float, default=0.0, help="Base order size in USDT (0 = use --wallet-pct)")
    p.add_argument("--wallet-pct", type=float, default=5.0, help="Entry size as %% of wallet balance when --base-size=0")
    p.add_argument("--comp-factor", type=float, default=1.0, help="USDT per %% band per base size (comp mode)")
    p.add_argument("--so-size", type=float, default=58.99, help="First/each DCA size (scale/flat modes)")
    p.add_argument("--volume-scale", type=float, default=1.3, help="Size multiplier per DCA (scale mode)")
    p.add_argument("--tp", type=float, default=0.5, help="Take-profit %% from average")
    p.add_argument("--leverage", type=float, default=10.0)
    # Execution (LIMIT orders on Binance Futures). Executes by default; use --dry-run to preview.
    p.add_argument("--dry-run", action="store_true", help="Preview only — do NOT place/replace any real orders")
    p.add_argument("--force", action="store_true", help="Place even if the symbol already has a position/open orders")
    p.add_argument("--tif", choices=["GTC", "GTX", "IOC", "FOK"], default="GTC", help="Time in force")
    p.add_argument("--position-mode", choices=["auto", "hedge", "oneway"], default="auto")
    p.add_argument("--set-leverage", type=int, default=0, help="Force a specific leverage (0=use symbol max)")
    p.add_argument("--no-max-leverage", action="store_true", help="Do NOT auto-set the symbol's max leverage")
    p.add_argument("--recv-window", type=int, default=5000)
    p.add_argument("--env-file", default=None, help="Path to .env with API keys (default: project root)")
    # Trailing TP on the opposite order book (AUTOMATIC by default when executing)
    p.add_argument("--no-tp", action="store_true", help="Do NOT auto-manage the trailing TP after placing the grid")
    p.add_argument("--tp-only", action="store_true", help="Skip the grid; only auto-manage the trailing TP for the position")
    p.add_argument("--supervise", action="store_true", help="Autonomous: re-arm the grid when flat + manage the trailing TP (loop)")
    p.add_argument("--tp-callback", type=float, default=0.2, help="Trailing callback rate %% (0.1..10)")
    p.add_argument("--tp-fee-buffer", type=float, default=0.12, help="Extra profit margin %% (fees+buffer) to stay green")
    p.add_argument("--tp-wall-min-mult", type=float, default=3.0, help="Min wall size vs median book qty to count as a wall")
    p.add_argument("--tp-wall-pick", choices=["nearest", "strongest"], default="nearest", help="Which opposite wall to target")
    p.add_argument("--tp-poll-sec", type=float, default=5.0, help="Position/TP re-sync interval (manage-tp)")
    p.add_argument("--keep-sl", action="store_true", help="Do NOT auto-cancel foreign STOP_MARKET SLs (e.g. Finandy's)")
    args = p.parse_args()
    # Executes by default; --dry-run flips it off.
    args.execute = not args.dry_run
    return args


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


def run_tp_manager(args: argparse.Namespace, is_long: bool | None) -> None:
    """Auto-manage the trailing TP for the live position (used standalone or after a grid)."""
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot manage trailing TP.{RESET}")
        return
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    hedge = _resolve_hedge(args, api, sec)

    # Direction: explicit flag pins it; otherwise leave it None so the manager
    # auto-detects the open side each pass (robust across restarts / either side).
    if is_long is None and args.direction in ("long", "short"):
        is_long = args.direction == "long"

    if not args.execute:
        # Dry-run: show the current live TP plan once (no order sent).
        side_is_long, qty, entry = _detect_open_side(args.symbol, hedge, api, sec, args.recv_window, is_long)
        depth = fetch_depth(args.symbol, args.limit)
        bids = [[float(p), float(q)] for p, q in depth["bids"]]
        asks = [[float(p), float(q)] for p, q in depth["asks"]]
        mid = (bids[0][0] + asks[0][0]) / 2
        if side_is_long is None:
            side_is_long = is_long if is_long is not None else (decide_direction(bids, asks, mid, args.auto_range)["direction"] == "long")
            print(f"{DIM}No open position on {args.symbol.upper()} — showing plan vs current price "
                  f"({'LONG' if side_is_long else 'SHORT'}).{RESET}")
        ref_avg = entry if qty > 0 else mid
        print_tp_plan(args.symbol.upper(), side_is_long, args, bids, asks, ref_avg, filt["tick_size"])
        print(f"\n{DIM}DRY-RUN — remove --dry-run to auto-manage (place/replace) the TP.{RESET}")
        return

    manage_trailing_tp(args.symbol, is_long, args, hedge, api, sec, filt)


def main() -> None:
    args = parse_args()

    if args.supervise:
        if not args.execute:
            print(f"{DIM}--supervise places/re-arms real orders; remove --dry-run to run it.{RESET}")
            return
        supervise_loop(args)
        return

    if args.tp_only:
        run_tp_manager(args, None)
        return

    try:
        depth = fetch_depth(args.symbol, args.limit)
    except Exception as exc:
        print(f"{RED}Could not fetch depth for {args.symbol.upper()}: {exc}{RESET}")
        return

    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]
    if not bids or not asks:
        print(f"{RED}Empty order book.{RESET}")
        return

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2

    if args.direction == "auto":
        d = decide_direction(bids, asks, mid, args.auto_range)
        args.direction = d["direction"]
        is_long = d["direction"] == "long"
        print(f"{BOLD}{CYAN}Auto-direction: {d['direction'].upper()}{RESET} "
              f"{DIM}(±{args.auto_range:g}% band · bid {d['bid_vol']:,.0f} vs ask {d['ask_vol']:,.0f} "
              f"· bid share {d['imbalance']*100:.1f}%){RESET}")
    else:
        is_long = args.direction == "long"

    entry = args.price if args.price is not None else mid

    # Resolve entry size (base) from wallet % and show the symbol's max leverage.
    if args.base_size <= 0 or not args.no_max_leverage:
        api, sec = load_keys(args.env_file)
        if not api or not sec:
            print(f"{RED}Need API keys to size from wallet %% / read max leverage. "
                  f"Pass --base-size or set keys.{RESET}")
            return
        if args.base_size <= 0:
            try:
                bal = get_wallet_balance(api, sec, args.recv_window)
                args.base_size = bal * args.wallet_pct / 100.0
                print(f"{BOLD}{CYAN}Entry size: {args.wallet_pct:g}% of wallet{RESET} "
                      f"{DIM}(wallet {bal:,.2f} USDT → {args.base_size:,.2f} USDT){RESET}")
            except Exception as exc:
                print(f"{RED}Could not read wallet balance: {exc}{RESET}")
                return
        if not args.no_max_leverage and args.set_leverage <= 0:
            try:
                args.leverage = get_max_leverage(args.symbol, api, sec, args.recv_window)
            except Exception:
                pass

    levels = bids if is_long else asks
    walls = select_walls(
        levels, entry, is_long, args.so_count, args.min_gap, args.min_dist, args.max_range
    )
    if not walls:
        print(f"{RED}No qualifying walls found. Adjust --min-gap/--min-dist/--max-range/--limit.{RESET}")
        return

    orders = build_grid(
        entry,
        is_long,
        walls,
        args.base_size,
        args.tp,
        args.size_mode,
        args.comp_factor,
        args.so_size,
        args.volume_scale,
    )
    print(render(args.symbol.upper(), args, orders, entry, len(walls)))

    # Prepare orders with exchange precision (price/qty rounding, min notional)
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    prepared = prepare_orders(orders, args.symbol, is_long, filt)

    side = "BUY" if is_long else "SELL"
    print(f"\n{BOLD}{CYAN}Orders to send ({side} LIMIT, rounded to exchange precision):{RESET}")
    for o in prepared:
        print(f"  {o['name']:<10} {side} {o['quantity']:>14} @ {o['price']:>13}  "
              f"(~{o['notional']:.2f} USDT)")

    # Trailing TP preview on the opposite order book (uses full-fill avg)
    print_tp_plan(args.symbol.upper(), is_long, args, bids, asks, orders[-1]["avg"], filt["tick_size"])

    if not args.execute:
        print(f"\n{DIM}DRY-RUN — no orders sent. Remove --dry-run to place the grid "
              f"and auto-manage the TP (or --no-tp to skip the TP).{RESET}")
        return

    placed_ok = place_orders(args.symbol, is_long, prepared, args)
    if not placed_ok:
        return

    # TP is AUTOMATIC: after placing the grid, keep the trailing TP synced to the position.
    if args.no_tp:
        print(f"\n{DIM}--no-tp set: skipping automatic TP management.{RESET}")
        return
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot auto-manage trailing TP.{RESET}")
        return
    hedge = _resolve_hedge(args, api, sec)
    manage_trailing_tp(args.symbol, is_long, args, hedge, api, sec, filt)


if __name__ == "__main__":
    main()
