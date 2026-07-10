#!/usr/bin/env python3
"""SPOT DCA grid anchored to REAL order book levels, with OCO (TP + SL) exits.

Sibling of orderbook_dca_grid.py, but for Binance **Spot** (api.binance.com):
  - Spot is LONG-only: the grid places BUY LIMIT orders on real BID walls below
    the entry price to accumulate the base asset (DCA on dips).
  - When the position is held, it maintains a single **OCO** SELL order
    (LIMIT_MAKER take-profit above + STOP_LOSS_LIMIT stop-loss below), so one
    leg cancels the other automatically.
  - No leverage, no shorting, no hedge mode (none exist on Spot).

Uses the SAME .env as the futures bot (BINANCE_API_KEY / BINANCE_SECRET_KEY);
the key just needs "Spot & Margin Trading" permission enabled.

Self-contained: Python standard library only.

Usage:
    python orderbook_dca_grid_spot.py ADAUSDT --dry-run   # preview (recommended first)
    python orderbook_dca_grid_spot.py ADAUSDT             # place grid + auto-manage OCO
    python orderbook_dca_grid_spot.py ADAUSDT --supervise # autonomous: re-arm + OCO
    python orderbook_dca_grid_spot.py ADAUSDT --tp-only   # only (re)place the OCO exit
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

SPOT_BASE = os.getenv("SPOT_BASE", "https://api.binance.com").rstrip("/")

# Binance Spot allows only these depth limits.
DEPTH_LIMITS = [5, 10, 20, 50, 100, 500, 1000, 5000]

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


# --- Env / keys ----------------------------------------------------------

def load_env_file(env_file: str | None) -> None:
    """Load a .env into os.environ without overwriting existing vars.

    Lookup order: --env-file → ./.env (cwd) → .env next to this script.
    """
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
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


def load_keys(env_file: str | None) -> tuple[str, str]:
    load_env_file(env_file)
    return os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", "")


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


# --- HTTP ----------------------------------------------------------------

def _public_get(path: str, params: dict) -> dict:
    url = f"{SPOT_BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _signed_request(method: str, path: str, params: dict, api: str, sec: str, recv_window: int) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window
    query = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{SPOT_BASE}{path}?{query}&signature={sig}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": api})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None


# --- Formatting ----------------------------------------------------------

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


def _clamp_depth_limit(limit: int) -> int:
    for allowed in DEPTH_LIMITS:
        if limit <= allowed:
            return allowed
    return DEPTH_LIMITS[-1]


def fetch_depth(symbol: str, limit: int) -> dict:
    return _public_get("/api/v3/depth", {"symbol": symbol.upper(), "limit": _clamp_depth_limit(limit)})


def best_book(symbol: str) -> tuple[float, float]:
    """Best bid/ask via the lightweight bookTicker (weight 1) — no deep depth."""
    t = _public_get("/api/v3/ticker/bookTicker", {"symbol": symbol.upper()})
    return float(t["bidPrice"]), float(t["askPrice"])


# --- Symbol filters ------------------------------------------------------

def load_symbol_filters(symbol: str) -> dict:
    info = _public_get("/api/v3/exchangeInfo", {"symbol": symbol.upper()})
    filt: dict = {
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "base_asset": "",
        "quote_asset": "USDT",
    }
    for item in info.get("symbols", []):
        if item.get("symbol") != symbol.upper():
            continue
        filt["base_asset"] = item.get("baseAsset", "")
        filt["quote_asset"] = item.get("quoteAsset", "USDT")
        for f in item.get("filters", []):
            ft = f.get("filterType")
            if ft == "PRICE_FILTER":
                filt["tick_size"] = Decimal(str(f.get("tickSize", "0.01")))
            elif ft == "LOT_SIZE":
                filt["step_size"] = Decimal(str(f.get("stepSize", "0.001")))
                filt["min_qty"] = Decimal(str(f.get("minQty", "0.001")))
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                filt["min_notional"] = Decimal(str(f.get("minNotional", "5")))
        break
    return filt


def _dec_places(step: Decimal) -> int:
    exp = step.normalize().as_tuple().exponent
    return max(0, -exp)


def _round_to(value: float, step: Decimal, rounding: str) -> Decimal:
    return (Decimal(str(value)) / step).to_integral_value(rounding=rounding) * step


# --- Grid (reused logic, LONG-only) --------------------------------------

def select_walls(
    levels: list[list[float]],
    entry: float,
    max_count: int,
    min_gap_pct: float,
    min_dist_pct: float,
    max_range_pct: float,
    wall_mult: float,
) -> list[tuple[float, float, float]]:
    """Detect the REAL bid walls below entry from the book itself.

    A level counts as a wall if its resting size is at least `wall_mult` × the
    median book size. We take as many such walls as exist (spaced by min-gap),
    so the number of DCA comes from the order book — not a fixed target.
    `max_count` is only an optional cap (0 = no cap).
    """
    qtys = [q for _, q in levels]
    med = statistics.median(qtys) if qtys else 0.0
    min_wall = med * wall_mult

    cands: list[tuple[float, float, float]] = []
    for price, qty in levels:
        if price >= entry:
            continue
        dist = abs(price - entry) / entry * 100
        if dist < min_dist_pct:
            continue
        if max_range_pct > 0 and dist > max_range_pct:
            continue
        if qty < min_wall:  # only genuine walls, not average book noise
            continue
        cands.append((price, qty, dist))

    cands.sort(key=lambda x: x[1], reverse=True)  # biggest walls first
    picked: list[tuple[float, float, float]] = []
    for price, qty, dist in cands:
        if all(abs(price - p) / entry * 100 >= min_gap_pct for p, _, _ in picked):
            picked.append((price, qty, dist))
        if max_count > 0 and len(picked) >= max_count:
            break
    picked.sort(key=lambda x: x[2])  # nearest to entry = DCA #1
    return picked


def build_grid(
    entry: float,
    walls: list[tuple[float, float, float]],
    base_size: float,
    tp_pct: float,
    size_mode: str,
    comp_factor: float,
    so_size: float,
    volume_scale: float,
) -> list[dict]:
    orders: list[dict] = [
        {"name": "Base Order", "price": entry, "wall_qty": None,
         "size_usdt": base_size, "qty": base_size / entry},
    ]
    prev_dist = 0.0
    for i, (price, wall_qty, dist) in enumerate(walls, start=1):
        if size_mode == "comp":
            band = max(dist - prev_dist, 0.0)
            size = base_size * band * comp_factor
        elif size_mode == "wall":
            size = None
        elif size_mode == "scale":
            size = so_size * (volume_scale ** (i - 1))
        else:
            size = so_size
        orders.append({"name": f"DCA #{i}", "price": price, "wall_qty": wall_qty,
                       "size_usdt": size, "dist": dist})
        prev_dist = dist

    if size_mode == "wall":
        total_wall = sum(w[1] for w in walls) or 1.0
        budget = base_size * len(walls)
        for o in orders[1:]:
            o["size_usdt"] = budget * (o["wall_qty"] / total_wall)

    for o in orders[1:]:
        o["qty"] = o["size_usdt"] / o["price"]

    cum_qty = cum_usdt = 0.0
    for o in orders:
        cum_qty += o["qty"]
        cum_usdt += o["size_usdt"]
        avg = cum_usdt / cum_qty
        o["cum_usdt"] = cum_usdt
        o["cum_qty"] = cum_qty
        o["avg"] = avg
        o["tp"] = avg * (1 + tp_pct / 100)
        o["delta_pct"] = (o["price"] / entry - 1) * 100
    return orders


def prepare_orders(orders: list[dict], filt: dict) -> list[dict]:
    """Round BUY LIMIT price/qty to exchange precision, enforce min qty/notional."""
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = _dec_places(tick)
    qty_dp = _dec_places(step)
    prepared: list[dict] = []
    for o in orders:
        price_d = _round_to(o["price"], tick, ROUND_DOWN)  # buy maker: below market
        qty_d = _round_to(o["qty"], step, ROUND_DOWN)
        if qty_d < filt["min_qty"]:
            qty_d = filt["min_qty"]
        while qty_d * price_d < filt["min_notional"]:
            qty_d += step
        prepared.append({
            "name": o["name"],
            "price": f"{price_d:.{price_dp}f}",
            "quantity": f"{qty_d:.{qty_dp}f}",
            "notional": float(price_d * qty_d),
        })
    return prepared


def render(symbol: str, args: argparse.Namespace, orders: list[dict], entry: float, found: int) -> str:
    lines: list[str] = []
    lines.append(f"{BOLD}{CYAN}SPOT OB DCA Grid · {symbol} · {GREEN}BUY{CYAN}{RESET}")
    lines.append(
        f"{DIM}entry {price_fmt(entry)}  ·  {found} DCA on BID walls below  ·  "
        f"size-mode {args.size_mode}  ·  TP +{args.tp:g}% / SL -{args.sl:g}% from avg  ·  "
        f"depth {args.limit}, min-gap {args.min_gap:g}%{RESET}"
    )
    lines.append("")
    header = (f"{'ORDER':<10} {'QTY':>12} {'PRICE':>13} {'Δ ENTRY':>9} "
              f"{'WALL':>12} {'SIZE USDT':>11} {'POS USDT':>11} {'AVG':>13} {'TP PRICE':>13}")
    lines.append(f"{DIM}{header}{RESET}")
    lines.append(f"{DIM}{'─' * len(header)}{RESET}")
    for o in orders:
        is_base = o["name"] == "Base Order"
        row_color = "" if is_base else GREEN
        wall = "—" if o["wall_qty"] is None else qty_fmt(o["wall_qty"])
        lines.append(
            f"{row_color}{o['name']:<10} {qty_fmt(o['qty']):>12} "
            f"{price_fmt(o['price']):>13} {o['delta_pct']:>+8.2f}% "
            f"{wall:>12} {o['size_usdt']:>11,.2f} {o['cum_usdt']:>11,.2f} "
            f"{price_fmt(o['avg']):>13} {price_fmt(o['tp']):>13}{RESET}"
        )
    last = orders[-1]
    lines.append("")
    lines.append(
        f"{BOLD}Full grid{RESET}  qty {qty_fmt(last['cum_qty'])}  ·  "
        f"cost {last['cum_usdt']:,.2f} USDT  ·  full-fill avg {price_fmt(last['avg'])}"
    )
    lines.append(
        f"{BOLD}Full-fill TP{RESET} {price_fmt(last['avg'] * (1 + args.tp / 100))} (+{args.tp:g}%)  ·  "
        f"{BOLD}SL{RESET} {price_fmt(last['avg'] * (1 - args.sl / 100))} (-{args.sl:g}%)"
    )
    cap = f" (cap {args.so_count})" if args.so_count > 0 else ""
    lines.append(
        f"{DIM}{found} DCA detected from real bid walls{cap} "
        f"(≥ {args.so_wall_mult:g}× median book size).{RESET}"
    )
    return "\n".join(lines)


# --- Account helpers -----------------------------------------------------

def get_free(api: str, sec: str, recv: int, asset: str) -> float:
    """Free (available) balance of an asset on the Spot account."""
    acc = _signed_request("GET", "/api/v3/account", {}, api, sec, recv)
    for b in acc.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0) or 0)
    return 0.0


def get_total(api: str, sec: str, recv: int, asset: str) -> float:
    """Total balance (free + locked) of an asset on the Spot account."""
    acc = _signed_request("GET", "/api/v3/account", {}, api, sec, recv)
    for b in acc.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
    return 0.0


def open_orders(symbol: str, api: str, sec: str, recv: int) -> list[dict]:
    try:
        return _signed_request("GET", "/api/v3/openOrders", {"symbol": symbol.upper()}, api, sec, recv) or []
    except Exception:
        return []


def existing_oco_sell(symbol: str, api: str, sec: str, recv: int) -> dict | None:
    """Return one leg of our open SELL OCO (orders belonging to an order list)."""
    for o in open_orders(symbol, api, sec, recv):
        if str(o.get("side", "")).upper() == "SELL" and int(o.get("orderListId", -1)) != -1:
            return o
    return None


def cancel_oco(symbol: str, order_list_id: int, api: str, sec: str, recv: int) -> None:
    _signed_request("DELETE", "/api/v3/orderList",
                    {"symbol": symbol.upper(), "orderListId": order_list_id}, api, sec, recv)


def average_cost(symbol: str, qty_target: float, api: str, sec: str, recv: int) -> float:
    """Approx average cost of the currently-held qty from the most recent BUY fills."""
    try:
        trades = _signed_request("GET", "/api/v3/myTrades",
                                 {"symbol": symbol.upper(), "limit": 200}, api, sec, recv)
    except Exception:
        return 0.0
    trades = sorted(trades or [], key=lambda t: t.get("time", 0), reverse=True)
    acc_qty = acc_cost = 0.0
    for t in trades:
        if not t.get("isBuyer"):
            continue
        q = float(t.get("qty", 0) or 0)
        p = float(t.get("price", 0) or 0)
        take = min(q, qty_target - acc_qty)
        if take <= 0:
            continue
        acc_qty += take
        acc_cost += take * p
        if acc_qty >= qty_target * 0.999:
            break
    return (acc_cost / acc_qty) if acc_qty > 0 else 0.0


def symbol_position_usdt(symbol: str, filt: dict, mid: float, api: str, sec: str, recv: int) -> float:
    """Current holding value in USDT (free base asset * mid price)."""
    base_qty = get_free(api, sec, recv, filt["base_asset"])
    return base_qty * mid


# --- Placement -----------------------------------------------------------

def place_buy_grid(symbol: str, prepared: list[dict], args: argparse.Namespace,
                   api: str, sec: str) -> bool:
    print(f"\n{BOLD}Placing {len(prepared)} BUY LIMIT orders on {symbol.upper()} (spot)…{RESET}")
    placed = 0
    for o in prepared:
        params = {
            "symbol": symbol.upper(),
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": args.tif,
            "quantity": o["quantity"],
            "price": o["price"],
        }
        try:
            resp = _signed_request("POST", "/api/v3/order", params, api, sec, args.recv_window)
            print(f"{GREEN}✓ {o['name']:<10} BUY {o['quantity']} @ {o['price']}  "
                  f"orderId={resp.get('orderId')}{RESET}")
            placed += 1
        except Exception as exc:
            print(f"{RED}✗ {o['name']:<10} BUY {o['quantity']} @ {o['price']}  → {exc}{RESET}")
    print(f"{BOLD}Placed {placed}/{len(prepared)} orders.{RESET}")
    return placed > 0


def choose_oco_prices(bids: list[list[float]], asks: list[list[float]], avg: float,
                      tp_pct: float, sl_pct: float, sl_buffer_pct: float, tick: Decimal,
                      wall_min_mult: float, pick: str,
                      grid_bottom: float | None = None) -> dict:
    """Anchor the OCO exit to real order-book walls.

    - TP (LIMIT_MAKER): a real ASK wall (resistance) at/above the profit floor
      avg*(1+tp%); if none is deep enough, falls back to that floor.
    - SL (STOP_LOSS_LIMIT): placed BELOW the whole DCA grid — under the deepest
      still-open DCA order (`grid_bottom`), snapped just under a support wall
      beneath it, with a `sl_buffer_pct` cushion. Only if the grid is fully
      filled (no open DCA) does it fall back to avg*(1-sl%).

    Both are clamped so the OCO is accepted (TP above best ask, SL below best bid).
    """
    tickf = float(tick)
    best_bid, best_ask = bids[0][0], asks[0][0]
    all_q = [q for _, q in bids] + [q for _, q in asks]
    med = statistics.median(all_q) if all_q else 0.0
    min_wall = med * wall_min_mult

    # --- Take-profit: nearest/strongest ASK wall at/above the profit floor ---
    tp_floor = max(avg * (1 + tp_pct / 100), best_ask + tickf)
    ask_walls = [(p, q) for p, q in asks if p >= tp_floor and q >= min_wall]
    if ask_walls:
        wall = (min(ask_walls, key=lambda x: x[0]) if pick == "nearest"
                else max(ask_walls, key=lambda x: x[1]))
        tp = wall[0]
        tp_wall_qty = wall[1]
    else:
        tp = tp_floor
        tp_wall_qty = None
    tp_d = _round_to(tp, tick, ROUND_UP)

    # --- Stop-loss: BELOW the DCA grid (only cut once the whole grid is broken) ---
    # Reference = deepest open DCA (grid bottom); fallback = avg*(1-sl%) when the
    # grid is fully filled and there is nothing left below to protect.
    sl_ref = grid_bottom if grid_bottom and grid_bottom < best_bid else avg * (1 - sl_pct / 100)
    # Snap the trigger just below the nearest support wall strictly beneath the grid.
    below_walls = [(p, q) for p, q in bids if p < sl_ref and q >= min_wall]
    if below_walls:
        wall = max(below_walls, key=lambda x: x[0])  # closest support below the grid
        stop = wall[0] - tickf
        sl_wall_qty = wall[1]
    else:
        stop = sl_ref * (1 - sl_buffer_pct / 100)
        sl_wall_qty = None
    stop = min(stop, sl_ref * (1 - sl_buffer_pct / 100), best_bid - tickf)  # below grid & last
    stop_d = _round_to(stop, tick, ROUND_DOWN)
    # SL limit a hair below the trigger so it fills once triggered.
    sl_limit_d = _round_to(float(stop_d) * (1 - 0.001), tick, ROUND_DOWN)
    if sl_limit_d >= stop_d:
        sl_limit_d = stop_d - tick

    return {"tp": tp_d, "stop": stop_d, "sl_limit": sl_limit_d,
            "tp_wall_qty": tp_wall_qty, "sl_wall_qty": sl_wall_qty}


def place_oco_sell(symbol: str, qty_str: str, prices: dict, filt: dict,
                   api: str, sec: str, recv: int) -> dict:
    price_dp = _dec_places(filt["tick_size"])
    params = {
        "symbol": symbol.upper(),
        "side": "SELL",
        "quantity": qty_str,
        "aboveType": "LIMIT_MAKER",
        "abovePrice": f"{prices['tp']:.{price_dp}f}",
        "belowType": "STOP_LOSS_LIMIT",
        "belowStopPrice": f"{prices['stop']:.{price_dp}f}",
        "belowPrice": f"{prices['sl_limit']:.{price_dp}f}",
        "belowTimeInForce": "GTC",
    }
    return _signed_request("POST", "/api/v3/orderList/oco", params, api, sec, recv)


def manage_oco_once(symbol: str, args: argparse.Namespace, filt: dict,
                    api: str, sec: str, verbose: bool = True) -> None:
    """Keep one SELL OCO (TP + SL) synced to the held base-asset quantity."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    base_qty = get_free(api, sec, args.recv_window, filt["base_asset"])

    # Lightweight best bid/ask (weight 1) — avoids pulling the deep book each poll.
    best_bid, best_ask = best_book(symbol)
    mid = (best_bid + best_ask) / 2

    min_notional = float(filt["min_notional"])
    holding_usdt = base_qty * mid
    if holding_usdt < min_notional:
        if verbose:
            print(f"{DIM}No sellable position yet ({qty_fmt(base_qty)} {filt['base_asset']} "
                  f"≈ {holding_usdt:,.2f} USDT).{RESET}")
        return

    # An OCO needs BOTH legs ≥ minNotional; the SL leg sits ~sl% lower, so the
    # holding must be a bit above minNotional to be placeable at all.
    min_needed = min_notional / max(1e-9, 1 - args.sl / 100) * 1.02
    if holding_usdt < min_needed:
        if verbose:
            print(f"{YELLOW}Holding ≈ {holding_usdt:,.2f} USDT too small for an OCO "
                  f"(stop leg would fall under minNotional {min_notional:g}). "
                  f"Need ≳ {min_needed:,.2f} USDT — raise --base-size / --wallet-pct.{RESET}")
        return

    qty_d = _round_to(base_qty, step, ROUND_DOWN)
    qty_str = f"{qty_d:.{qty_dp}f}"

    existing = existing_oco_sell(symbol, api, sec, args.recv_window)
    if existing:
        try:
            same = abs(float(existing.get("origQty", 0) or 0) - float(qty_str)) < float(step) / 2
        except (TypeError, ValueError):
            same = False
        if same:
            return  # OCO already covers the current holding — nothing to do (no deep fetch)
        try:
            cancel_oco(symbol, int(existing.get("orderListId")), api, sec, args.recv_window)
            print(f"{DIM}Holding changed → replacing OCO with qty {qty_str}{RESET}")
        except Exception as exc:
            print(f"{RED}Cancel old OCO failed: {exc}{RESET}")

    # Only now (placing/replacing) pull the deep book to anchor TP/SL to real walls.
    depth = fetch_depth(symbol, args.limit)
    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]

    # Grid bottom = deepest still-open DCA buy → the SL goes BELOW it.
    buys = [float(o.get("price", 0) or 0) for o in open_orders(symbol, api, sec, args.recv_window)
            if str(o.get("side", "")).upper() == "BUY"]
    grid_bottom = min(buys) if buys else None

    avg = average_cost(symbol, float(qty_str), api, sec, args.recv_window)
    if avg <= 0:
        avg = mid  # fallback: no trade history found
    prices = choose_oco_prices(bids, asks, avg, args.tp, args.sl, args.sl_buffer,
                               filt["tick_size"], args.tp_wall_min_mult, args.tp_wall_pick,
                               grid_bottom=grid_bottom)

    # Both OCO legs must clear minNotional (the lower SL leg is the binding one).
    leg_min = min(float(prices["tp"]), float(prices["sl_limit"])) * float(qty_str)
    if leg_min < min_notional:
        if verbose:
            print(f"{YELLOW}OCO leg would be {leg_min:,.2f} < minNotional {min_notional:g} "
                  f"(SL sits below the grid). Position too small — raise --base-size.{RESET}")
        return

    try:
        resp = place_oco_sell(symbol, qty_str, prices, filt, api, sec, args.recv_window)
        oid = resp.get("orderListId")
        tp_note = f"on {qty_fmt(prices['tp_wall_qty'])} wall" if prices["tp_wall_qty"] else "profit floor"
        sl_ref_note = "below grid" if grid_bottom else "risk cap"
        sl_note = (f"under {qty_fmt(prices['sl_wall_qty'])} wall, {sl_ref_note}"
                   if prices["sl_wall_qty"] else sl_ref_note)
        print(f"{GREEN}✓ OCO SELL {qty_str} · TP {price_fmt(float(prices['tp']))} ({tp_note}) · "
              f"SL {price_fmt(float(prices['stop']))} ({sl_note}) · avg {price_fmt(avg)} · "
              f"listId={oid}{RESET}")
    except Exception as exc:
        print(f"{RED}✗ Place OCO failed: {exc}{RESET}")


# --- Guards --------------------------------------------------------------

def symbol_cap_blocks(symbol: str, args: argparse.Namespace, filt: dict, mid: float,
                      add_usdt: float, api: str, sec: str, verbose: bool = True) -> bool:
    """True if current holding + add would exceed the per-symbol cap.

    The cap is `--max-symbol-pct`% of the wallet (total USDT) if set, otherwise the
    absolute `--max-symbol-usdt`. 0/unset on both = no cap.
    """
    pct = getattr(args, "max_symbol_pct", 0.0)
    abs_cap = getattr(args, "max_symbol_usdt", 0.0)
    try:
        cap = 0.0
        cap_desc = ""
        if pct > 0:
            wallet = get_total(api, sec, args.recv_window, filt["quote_asset"])
            cap = wallet * pct / 100.0
            cap_desc = f"{pct:g}% of {wallet:,.2f} = {cap:,.2f} USDT"
        elif abs_cap > 0:
            cap = abs_cap
            cap_desc = f"{cap:,.2f} USDT"
        if cap <= 0:
            return False
        held = symbol_position_usdt(symbol, filt, mid, api, sec, args.recv_window)
    except Exception as exc:
        if verbose:
            print(f"{YELLOW}Could not read wallet/holding ({exc}); skipping symbol cap.{RESET}")
        return False
    if held + add_usdt <= cap:
        return False
    if verbose:
        if args.force:
            print(f"{YELLOW}Symbol cap ({cap_desc}) would be exceeded "
                  f"(held {held:,.2f} + {add_usdt:,.2f}) — continuing due to --force.{RESET}")
        else:
            print(f"{RED}Symbol cap reached: held {held:,.2f} + {add_usdt:,.2f} > {cap_desc}. "
                  f"Skipping (use --force or set --max-symbol-pct/--max-symbol-usdt 0).{RESET}")
    return not args.force


# --- Grid build & place --------------------------------------------------

def build_and_place_grid(args: argparse.Namespace, api: str, sec: str, filt: dict,
                         verbose: bool = True) -> bool:
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
    entry = args.price if args.price is not None else mid

    base_size = args.base_size
    if base_size <= 0:
        try:
            quote_free = get_free(api, sec, args.recv_window, filt["quote_asset"])
            base_size = quote_free * args.wallet_pct / 100.0
        except Exception as exc:
            print(f"{RED}Quote balance read failed: {exc}{RESET}")
            return False
        if base_size < args.min_base_usdt:
            if verbose:
                print(f"{DIM}Wallet {args.wallet_pct:g}% = {base_size:,.2f} USDT < floor "
                      f"{args.min_base_usdt:g} → using {args.min_base_usdt:g} USDT.{RESET}")
            base_size = args.min_base_usdt

    if symbol_cap_blocks(args.symbol, args, filt, mid, base_size, api, sec, verbose):
        return False

    walls = select_walls(bids, entry, args.so_count, args.min_gap, args.min_dist,
                         args.max_range, args.so_wall_mult)
    if not walls:
        print(f"{RED}No qualifying bid walls found (lower --so-wall-mult/--min-gap or raise --max-range).{RESET}")
        return False

    orders = build_grid(entry, walls, base_size, args.tp, args.size_mode,
                        args.comp_factor, args.so_size, args.volume_scale)
    prepared = prepare_orders(orders, filt)
    return place_buy_grid(args.symbol, prepared, args, api, sec)


# --- Loops ---------------------------------------------------------------

def supervise_loop(args: argparse.Namespace) -> None:
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot supervise.{RESET}")
        return
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    print(f"\n{BOLD}{CYAN}Supervising SPOT {args.symbol.upper()} "
          f"(auto re-arm buy grid + OCO exit, poll {args.poll_sec:g}s). Ctrl+C to stop.{RESET}")
    try:
        while True:
            sleep_s = args.poll_sec
            try:
                best_bid, best_ask = best_book(args.symbol)
                mid = (best_bid + best_ask) / 2
                base_qty = get_free(api, sec, args.recv_window, filt["base_asset"])
                holding_usdt = base_qty * mid

                if holding_usdt >= float(filt["min_notional"]):
                    # We hold the asset → keep the OCO (TP + SL) synced.
                    manage_oco_once(args.symbol, args, filt, api, sec, verbose=True)
                else:
                    oo = open_orders(args.symbol, api, sec, args.recv_window)
                    buys = [o for o in oo if str(o.get("side", "")).upper() == "BUY"]
                    if buys:
                        print(f"{DIM}Flat · grid armed ({len(buys)} buy orders waiting to fill)…{RESET}")
                    else:
                        print(f"{BOLD}Flat and no orders → re-arming buy grid…{RESET}")
                        placed = build_and_place_grid(args, api, sec, filt, verbose=True)
                        if not placed:
                            sleep_s = max(args.poll_sec, args.rearm_backoff)
                            print(f"{DIM}Could not arm grid → retrying in {sleep_s:g}s.{RESET}")
            except Exception as exc:
                print(f"{RED}Supervisor pass error: {exc}{RESET}")
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped supervising (open orders/OCO left in place).")


def run_oco_manager(args: argparse.Namespace) -> None:
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}No API keys — cannot manage OCO.{RESET}")
        return
    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return
    if not args.execute:
        print(f"{DIM}DRY-RUN — showing what the OCO would look like (no order sent).{RESET}")
        depth = fetch_depth(args.symbol, args.limit)
        bids = [[float(p), float(q)] for p, q in depth["bids"]]
        asks = [[float(p), float(q)] for p, q in depth["asks"]]
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2
        base_qty = get_free(api, sec, args.recv_window, filt["base_asset"])
        if base_qty * mid < float(filt["min_notional"]):
            print(f"{DIM}No sellable position ({qty_fmt(base_qty)} {filt['base_asset']}).{RESET}")
            return
        avg = average_cost(args.symbol, base_qty, api, sec, args.recv_window) or mid
        buys = [float(o.get("price", 0) or 0) for o in open_orders(args.symbol, api, sec, args.recv_window)
                if str(o.get("side", "")).upper() == "BUY"]
        grid_bottom = min(buys) if buys else None
        prices = choose_oco_prices(bids, asks, avg, args.tp, args.sl, args.sl_buffer,
                                   filt["tick_size"], args.tp_wall_min_mult, args.tp_wall_pick,
                                   grid_bottom=grid_bottom)
        tp_note = f"on {qty_fmt(prices['tp_wall_qty'])} ask wall" if prices["tp_wall_qty"] else f"profit floor +{args.tp:g}%"
        sl_note = ("below grid" if grid_bottom else f"risk cap -{args.sl:g}%")
        print(f"  SELL {qty_fmt(base_qty)} {filt['base_asset']} · avg {price_fmt(avg)}")
        print(f"  TP {price_fmt(float(prices['tp']))} ({tp_note})  ·  "
              f"SL {price_fmt(float(prices['stop']))} ({sl_note})")
        return
    print(f"\n{BOLD}{CYAN}Managing OCO on SPOT {args.symbol.upper()} "
          f"(TP +{args.tp:g}% / SL -{args.sl:g}%, poll {args.poll_sec:g}s). Ctrl+C to stop.{RESET}")
    try:
        while True:
            try:
                manage_oco_once(args.symbol, args, filt, api, sec, verbose=True)
            except Exception as exc:
                print(f"{RED}OCO pass error: {exc}{RESET}")
            time.sleep(args.poll_sec)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped managing OCO (existing OCO left in place).")


# --- CLI -----------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    env_file = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--env-file" and i + 1 < len(argv):
            env_file = argv[i + 1]
        elif a.startswith("--env-file="):
            env_file = a.split("=", 1)[1]
    load_env_file(env_file)

    p = argparse.ArgumentParser(description="SPOT DCA buy-grid anchored to real order-book walls, with OCO (TP+SL) exit")
    p.add_argument("symbol", help="Spot symbol, e.g. ADAUSDT")
    p.add_argument("--price", type=float, default=None, help="Entry price (default: live mid)")
    p.add_argument("--so-count", type=int, default=_env_int("SO_MAX", 15),
                   help="MAX cap on DCA orders (not a target). The count comes from real walls "
                        "in the book; fewer walls → fewer DCA. 0 = no cap. Env: SO_MAX")
    p.add_argument("--so-wall-mult", type=float, default=_env_float("SO_WALL_MULT", 2.0),
                   help="A bid level counts as a wall (DCA) if its size ≥ this × median book size. Env: SO_WALL_MULT")
    p.add_argument("--limit", type=int, default=5000, help="Order book depth to fetch (deep = reaches walls on pricey/liquid coins)")
    p.add_argument("--min-gap", type=float, default=0.2,
                   help="Min %% spacing between chosen walls (lower = more DCA on dense books like BTC/ETH)")
    p.add_argument("--min-dist", type=float, default=0.1, help="Min %% distance of first wall from entry")
    p.add_argument("--max-range", type=float, default=15.0, help="Only DCA walls within this %% of entry (0=off)")
    p.add_argument("--size-mode", choices=["comp", "wall", "scale", "flat"], default="comp",
                   help="comp=distance compensation, wall=∝ wall liquidity, scale=geometric, flat=equal")
    p.add_argument("--base-size", type=float, default=_env_float("BASE_SIZE", 0.0),
                   help="Base order size in USDT (0 = use --wallet-pct). Env: BASE_SIZE")
    p.add_argument("--wallet-pct", type=float, default=_env_float("WALLET_PCT", 10.0),
                   help="Entry size as %% of free quote (USDT) when --base-size=0. Env: WALLET_PCT")
    p.add_argument("--min-base-usdt", type=float, default=_env_float("MIN_BASE_USDT", 10.0),
                   help="Floor for the wallet-%% entry size: if the %% is below this, use this "
                        "USDT instead. Env: MIN_BASE_USDT")
    p.add_argument("--comp-factor", type=float, default=1.0, help="USDT per %% band per base size (comp mode)")
    p.add_argument("--so-size", type=float, default=58.99, help="First/each DCA size (scale/flat modes)")
    p.add_argument("--volume-scale", type=float, default=1.3, help="Size multiplier per DCA (scale mode)")
    p.add_argument("--tp", type=float, default=_env_float("SPOT_TP", 0.5),
                   help="Min take-profit %% above avg (profit floor; TP anchors to an ask wall at/above it). Env: SPOT_TP")
    p.add_argument("--sl", type=float, default=_env_float("SPOT_SL", 5.0),
                   help="Fallback stop-loss %% below avg when the grid is fully filled (no open DCA). Env: SPOT_SL")
    p.add_argument("--sl-buffer", type=float, default=_env_float("SPOT_SL_BUFFER", 0.5),
                   help="Extra %% below the deepest DCA (grid bottom) for the stop trigger. Env: SPOT_SL_BUFFER")
    p.add_argument("--tp-wall-min-mult", type=float, default=3.0,
                   help="Min wall size vs median book qty to count as a wall for TP/SL anchoring")
    p.add_argument("--tp-wall-pick", choices=["nearest", "strongest"], default="nearest",
                   help="Which order-book wall to anchor the TP/SL to")
    p.add_argument("--max-symbol-pct", type=float, default=_env_float("MAX_SYMBOL_PCT", 50.0),
                   help="Max invested per symbol as %% of wallet (total USDT). Takes precedence over "
                        "--max-symbol-usdt. Default 50. 0=off. Env: MAX_SYMBOL_PCT")
    p.add_argument("--max-symbol-usdt", type=float, default=_env_float("MAX_SYMBOL_USDT", 0.0),
                   help="Max USDT invested per symbol (absolute; used if --max-symbol-pct=0). 0=off. Env: MAX_SYMBOL_USDT")
    p.add_argument("--dry-run", action="store_true", help="Preview only — do NOT place any real orders")
    p.add_argument("--force", action="store_true", help="Place even if the symbol already has holding/open orders")
    p.add_argument("--tif", choices=["GTC", "IOC", "FOK"], default="GTC", help="Time in force for buy limits")
    p.add_argument("--recv-window", type=int, default=5000)
    p.add_argument("--env-file", default=None, help="Path to .env with API keys (default: project root)")
    p.add_argument("--no-tp", action="store_true", help="Do NOT auto-manage the OCO after placing the grid")
    p.add_argument("--tp-only", action="store_true", help="Skip the grid; only (re)place/manage the OCO exit")
    p.add_argument("--supervise", action="store_true", help="Autonomous: re-arm grid when flat + manage OCO (loop)")
    p.add_argument("--poll-sec", type=float, default=5.0, help="Position/OCO re-sync interval")
    p.add_argument("--rearm-backoff", type=float, default=_env_float("REARM_BACKOFF", 60.0),
                   help="When flat but a grid can't be armed, wait this long before retrying. Env: REARM_BACKOFF")
    args = p.parse_args()
    args.execute = not args.dry_run
    return args


def main() -> None:
    args = parse_args()

    if args.supervise:
        if not args.execute:
            print(f"{DIM}--supervise places/re-arms real orders; remove --dry-run to run it.{RESET}")
            return
        supervise_loop(args)
        return

    if args.tp_only:
        run_oco_manager(args)
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
    mid = (bids[0][0] + asks[0][0]) / 2
    entry = args.price if args.price is not None else mid

    try:
        filt = load_symbol_filters(args.symbol)
    except Exception as exc:
        print(f"{RED}Could not load symbol filters: {exc}{RESET}")
        return

    # Keys are needed to trade, to size from wallet %, and for account guards.
    # A pure --dry-run with an explicit --base-size can preview without them.
    need_keys = args.execute or args.base_size <= 0 or args.max_symbol_usdt > 0
    api, sec = load_keys(args.env_file)
    if need_keys and (not api or not sec):
        print(f"{RED}Need API keys (env or .env) to size from wallet %% and trade.{RESET}")
        return

    base_size = args.base_size
    if base_size <= 0:
        try:
            quote_free = get_free(api, sec, args.recv_window, filt["quote_asset"])
            base_size = quote_free * args.wallet_pct / 100.0
            note = ""
            if base_size < args.min_base_usdt:
                note = f" → floored to {args.min_base_usdt:g}"
                base_size = args.min_base_usdt
            args.base_size = base_size
            print(f"{BOLD}{CYAN}Entry size: {args.wallet_pct:g}% of free {filt['quote_asset']}{RESET} "
                  f"{DIM}({quote_free:,.2f} → {base_size:,.2f} USDT{note}){RESET}")
        except Exception as exc:
            print(f"{RED}Could not read quote balance: {exc}{RESET}")
            return

    # Safety: don't stack a new grid on top of existing holding/orders.
    if args.execute and not args.force:
        held_usdt = symbol_position_usdt(args.symbol, filt, mid, api, sec, args.recv_window)
        oo = open_orders(args.symbol, api, sec, args.recv_window)
        if held_usdt >= float(filt["min_notional"]) or oo:
            detail = []
            if held_usdt >= float(filt["min_notional"]):
                detail.append(f"holding ≈ {held_usdt:,.2f} USDT")
            if oo:
                detail.append(f"{len(oo)} open order(s)")
            print(f"{RED}✗ {args.symbol.upper()} already has exposure ({', '.join(detail)}). "
                  f"Not placing a new grid. Use --force, or --tp-only to just manage the OCO.{RESET}")
            return

    if symbol_cap_blocks(args.symbol, args, filt, mid, base_size, api, sec):
        return

    walls = select_walls(bids, entry, args.so_count, args.min_gap, args.min_dist,
                         args.max_range, args.so_wall_mult)
    if not walls:
        print(f"{RED}No qualifying bid walls found. Lower --so-wall-mult/--min-gap or raise --max-range.{RESET}")
        return

    orders = build_grid(entry, walls, base_size, args.tp, args.size_mode,
                        args.comp_factor, args.so_size, args.volume_scale)
    print(render(args.symbol.upper(), args, orders, entry, len(walls)))

    prepared = prepare_orders(orders, filt)
    print(f"\n{BOLD}{CYAN}Orders to send (BUY LIMIT, rounded to exchange precision):{RESET}")
    for o in prepared:
        print(f"  {o['name']:<10} BUY {o['quantity']:>14} @ {o['price']:>13}  (~{o['notional']:.2f} USDT)")

    if not args.execute:
        print(f"\n{DIM}DRY-RUN — no orders sent. Remove --dry-run to place the grid "
              f"and auto-manage the OCO (or --no-tp to skip it).{RESET}")
        return

    if not place_buy_grid(args.symbol, prepared, args, api, sec):
        return

    if args.no_tp:
        print(f"\n{DIM}--no-tp set: skipping automatic OCO management.{RESET}")
        return
    print(f"\n{BOLD}{CYAN}Managing OCO on SPOT {args.symbol.upper()} "
          f"(TP +{args.tp:g}% / SL -{args.sl:g}%, poll {args.poll_sec:g}s). Ctrl+C to stop.{RESET}")
    try:
        while True:
            try:
                manage_oco_once(args.symbol, args, filt, api, sec, verbose=True)
            except Exception as exc:
                print(f"{RED}OCO pass error: {exc}{RESET}")
            time.sleep(args.poll_sec)
    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped managing OCO (open orders/OCO left in place).")


if __name__ == "__main__":
    main()
