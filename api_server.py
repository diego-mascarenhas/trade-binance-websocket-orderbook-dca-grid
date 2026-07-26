#!/usr/bin/env python3
"""HTTP control API for Orderbook Trading (iOS) — scan + DCA botctl."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import botctl  # noqa: E402
import futures_scan  # noqa: E402
from orderbook_dca_grid import (  # noqa: E402
    _signed_request,
    load_env_file,
    load_keys,
)

SYM_RE = re.compile(r"^[A-Z0-9]{4,32}$")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def list_open_positions() -> list[dict[str, Any]]:
    """Open USDT-M Futures positions from Binance positionRisk."""
    api, sec = load_keys(None)
    if not api or not sec:
        raise RuntimeError("No API keys in .env (BINANCE_API_KEY / BINANCE_SECRET_KEY)")
    recv = int(_env("RECV_WINDOW", "15000") or "15000")
    rows = _signed_request("GET", "/fapi/v2/positionRisk", {}, api, sec, recv)
    running = {s.upper() for s in botctl.list_running()}
    out: list[dict[str, Any]] = []
    for r in rows if isinstance(rows, list) else []:
        amt = float(r.get("positionAmt", 0) or 0)
        if abs(amt) <= 0:
            continue
        sym = str(r.get("symbol", "")).upper()
        if not sym:
            continue
        entry = float(r.get("entryPrice", 0) or 0)
        mark = float(r.get("markPrice", 0) or 0)
        pnl = float(r.get("unRealizedProfit", 0) or 0)
        raw_n = r.get("notional", "")
        if raw_n not in ("", None):
            notional = abs(float(raw_n))
        else:
            notional = abs(amt) * (mark if mark > 0 else entry)
        side = "LONG" if amt > 0 else "SHORT"
        pos_side = str(r.get("positionSide", "BOTH")).upper()
        if pos_side == "LONG":
            side = "LONG"
        elif pos_side == "SHORT":
            side = "SHORT"
        lev = int(float(r.get("leverage", 0) or 0))
        liq = float(r.get("liquidationPrice", 0) or 0)
        pnl_pct = (pnl / notional * 100) if notional > 0 else 0.0
        out.append(
            {
                "symbol": sym,
                "side": side,
                "position_side": pos_side,
                "qty": abs(amt),
                "entry": entry,
                "mark": mark,
                "notional": notional,
                "leverage": lev,
                "unrealized_pnl": pnl,
                "pnl_pct": pnl_pct,
                "liquidation": liq,
                "bot_running": sym in running,
                "tv_symbol": f"BINANCE:{sym}.P",
            }
        )
    out.sort(key=lambda p: abs(float(p["unrealized_pnl"])), reverse=True)
    return out


def _f(v: Any) -> float | None:
    if v in (None, "", "0", 0, "0.0"):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def _level(kind: str, label: str, price: float, color: str) -> dict[str, Any]:
    return {"kind": kind, "label": label, "price": price, "color": color}


def _strongest_wall(levels: list[list[float]], *, below: float | None, above: float | None) -> tuple[float, float] | None:
    """Return (price, qty) of the largest resting wall on one side of mid."""
    best: tuple[float, float] | None = None
    for p, q in levels:
        if below is not None and p >= below:
            continue
        if above is not None and p <= above:
            continue
        if best is None or q > best[1]:
            best = (p, q)
    return best


def _impulse_pivots(candles: list[dict[str, float]], mid: float) -> dict[str, float | None]:
    """Recent swing high/low used as impulse origin for gate suggestions."""
    if len(candles) < 8:
        return {"high": None, "low": None}
    window = candles[-24:]
    hi = max(c["high"] for c in window)
    lo = min(c["low"] for c in window)
    # Prefer pivots on the meaningful side of mid (impulse already happened)
    swing_high = hi if hi > mid else None
    swing_low = lo if lo < mid else None
    return {"high": swing_high, "low": swing_low}


def gate_suggestions(
    *,
    base: str,
    symbol: str,
    candles: list[dict[str, float]],
    mid: float | None = None,
) -> dict[str, Any]:
    """Suggest gate prices from OB walls + impulse pivots.

    SHORT arms only when mid > gate → gate below mid (reclaim / dump origin).
    LONG arms only when mid < gate → gate above mid (fade / pump origin).
    """
    sym = symbol.upper()
    depth_raw = futures_scan._get(
        f"{base}/fapi/v1/depth?{urlencode({'symbol': sym, 'limit': 100})}"
    )
    depth = depth_raw if isinstance(depth_raw, dict) else {}
    bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]
    if mid is None or mid <= 0:
        mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0.0

    bid_wall = _strongest_wall(bids, below=mid, above=None)
    ask_wall = _strongest_wall(asks, below=None, above=mid)
    pivots = _impulse_pivots(candles, mid)

    short_opts: list[dict[str, Any]] = []
    if pivots["low"]:
        short_opts.append(
            {
                "price": float(pivots["low"]),
                "source": "impulse",
                "label": "Impulse low",
                "note": "SHORT only if mid > this reclaim level",
            }
        )
    if bid_wall:
        short_opts.append(
            {
                "price": float(bid_wall[0]),
                "source": "ob_wall",
                "label": "Bid wall",
                "note": f"Strongest bid below mid (qty {bid_wall[1]:g})",
            }
        )

    long_opts: list[dict[str, Any]] = []
    if pivots["high"]:
        long_opts.append(
            {
                "price": float(pivots["high"]),
                "source": "impulse",
                "label": "Impulse high",
                "note": "LONG only if mid < this fade level",
            }
        )
    if ask_wall:
        long_opts.append(
            {
                "price": float(ask_wall[0]),
                "source": "ob_wall",
                "label": "Ask wall",
                "note": f"Strongest ask above mid (qty {ask_wall[1]:g})",
            }
        )

    book = futures_scan.book_imbalance(bids, asks, mid) if bids and asks else {"direction": "LONG"}
    return {
        "mid": mid,
        "book_direction": book.get("direction", "LONG"),
        "short": short_opts[0] if short_opts else None,
        "long": long_opts[0] if long_opts else None,
        "short_options": short_opts,
        "long_options": long_opts,
        "bid_wall": {"price": bid_wall[0], "qty": bid_wall[1]} if bid_wall else None,
        "ask_wall": {"price": ask_wall[0], "qty": ask_wall[1]} if ask_wall else None,
        "impulse_high": pivots["high"],
        "impulse_low": pivots["low"],
    }


def chart_payload(symbol: str, *, interval: str = "15m", limit: int = 120) -> dict[str, Any]:
    """Candles + OPEN / LIMIT / TP / SL levels for the lightweight chart."""
    sym = symbol.upper()
    api, sec = load_keys(None)
    recv = int(_env("RECV_WINDOW", "15000") or "15000")
    base = _env("FAPI_BASE", futures_scan.FAPI_BASE) or futures_scan.FAPI_BASE

    raw_klines = futures_scan.fetch_klines(base, sym, interval, max(20, min(int(limit), 500)))
    candles: list[dict[str, float]] = []
    for k in raw_klines:
        candles.append(
            {
                "time": int(k[0]) // 1000,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            }
        )

    levels: list[dict[str, Any]] = []
    side = ""
    entry = 0.0
    mark = 0.0
    notional = 0.0
    unrealized_pnl = 0.0
    # Signed account data is optional: candles (public) must still render if
    # keys are missing or Binance rejects them (IP whitelist, permissions, etc.).
    if not api or not sec:
        sys.stderr.write(f"chart {sym}: no API keys — candles only\n")
    try:
        if not api or not sec:
            raise RuntimeError("No API keys in .env")
        rows = _signed_request(
            "GET", "/fapi/v2/positionRisk", {"symbol": sym}, api, sec, recv
        )
        for r in rows if isinstance(rows, list) else []:
            amt = float(r.get("positionAmt", 0) or 0)
            if abs(amt) <= 0:
                continue
            pos_side = str(r.get("positionSide", "BOTH")).upper()
            if pos_side == "LONG" or (pos_side == "BOTH" and amt > 0):
                side = "LONG"
            else:
                side = "SHORT"
            entry = float(r.get("entryPrice", 0) or 0)
            mark = float(r.get("markPrice", 0) or 0)
            unrealized_pnl = float(r.get("unRealizedProfit", 0) or 0)
            raw_n = r.get("notional", "")
            if raw_n not in ("", None):
                notional = abs(float(raw_n))
            else:
                notional = abs(amt) * (mark if mark > 0 else entry)
            if entry > 0:
                levels.append(_level("open", "OPEN", entry, "#c084fc"))
            if mark > 0:
                levels.append(_level("mark", "MARK", mark, "#8b949e"))
            liq = _f(r.get("liquidationPrice"))
            if liq:
                levels.append(_level("liq", "LIQ", liq, "#fb923c"))
            break
    except Exception as exc:
        sys.stderr.write(f"chart {sym}: positionRisk skipped: {exc}\n")

    pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0.0

    quote_volume = 0.0
    try:
        tickers = futures_scan.fetch_tickers(base, {sym})
        if tickers:
            quote_volume = float(tickers[0].quote_volume)
    except Exception:
        pass

    try:
        oo = (
            _signed_request(
                "GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, recv
            )
            or []
        )
        limit_i = 0
        for o in oo if isinstance(oo, list) else []:
            otype = str(o.get("type", "")).upper()
            price = _f(o.get("price"))
            if not price:
                continue
            reduce_only = str(o.get("reduceOnly", "false")).lower() in ("true", "1")
            o_side = str(o.get("side", "")).upper()
            if otype in ("LIMIT", "LIMIT_MAKER"):
                if reduce_only:
                    # Closing LIMIT often used as manual TP
                    kind, label, color = "tp", "TP", "#3fb950"
                    if side == "LONG" and o_side == "SELL":
                        pass
                    elif side == "SHORT" and o_side == "BUY":
                        pass
                    else:
                        kind, label, color = "limit", "LIMIT", "#58a6ff"
                else:
                    limit_i += 1
                    kind, label, color = "limit", f"LIMIT {limit_i}", "#58a6ff"
                levels.append(_level(kind, label, price, color))
            elif otype in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
                tp = _f(o.get("stopPrice")) or price
                if tp:
                    levels.append(_level("tp", "TP", tp, "#3fb950"))
            elif otype in ("STOP", "STOP_MARKET"):
                sl = _f(o.get("stopPrice")) or price
                if sl:
                    levels.append(_level("sl", "SL", sl, "#f85149"))
    except Exception as exc:
        sys.stderr.write(f"chart {sym}: openOrders skipped: {exc}\n")

    try:
        from orderbook_staged_exit import list_open_algo_orders, _algo_client_id
    except ImportError:
        list_open_algo_orders = None  # type: ignore[assignment]
        _algo_client_id = None  # type: ignore[assignment]

    if list_open_algo_orders is not None:
        try:
            for o in list_open_algo_orders(sym, api, sec, recv):
                otype = str(o.get("orderType") or o.get("type") or "").upper()
                trig = (
                    _f(o.get("triggerPrice"))
                    or _f(o.get("stopPrice"))
                    or _f(o.get("activatePrice"))
                    or _f(o.get("price"))
                )
                if not trig:
                    continue
                cid = ""
                if _algo_client_id is not None:
                    try:
                        cid = str(_algo_client_id(o) or "")
                    except Exception:
                        cid = str(o.get("clientAlgoId") or o.get("clientOrderId") or "")
                tag = cid.upper()
                if "TP1" in tag or otype.startswith("TAKE_PROFIT"):
                    levels.append(_level("tp", "TP", trig, "#3fb950"))
                elif "BE" in tag or tag.endswith("SL") or "SL" in tag or otype in (
                    "STOP", "STOP_MARKET", "STOP_LOSS", "STOP_LOSS_MARKET",
                ):
                    levels.append(_level("sl", "SL", trig, "#f85149"))
                elif "TR" in tag or "TRAIL" in otype or otype == "TRAILING_STOP_MARKET":
                    levels.append(_level("trail", "TRAIL", trig, "#fbbf24"))
                elif otype.startswith("TAKE_PROFIT"):
                    levels.append(_level("tp", "TP", trig, "#3fb950"))
                else:
                    levels.append(_level("limit", "ALGO", trig, "#58a6ff"))
        except Exception as exc:
            sys.stderr.write(f"chart {sym}: algo orders skipped: {exc}\n")

    # Deduplicate near-identical prices per kind
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for lv in levels:
        key = (str(lv["kind"]), int(round(float(lv["price"]) * 1e8)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lv)

    mid_for_gate = mark if mark > 0 else (candles[-1]["close"] if candles else 0.0)
    try:
        gates = gate_suggestions(base=base, symbol=sym, candles=candles, mid=mid_for_gate)
    except Exception:
        gates = {"mid": mid_for_gate, "short": None, "long": None}

    active_gate = None
    bot_direction = None
    bot_running = False
    gate_enabled = False
    try:
        bot_running = botctl.is_running(sym)
        meta = botctl.run_meta(sym)
        bot_direction = meta.get("direction")
        g = meta.get("gate_price")
        if g is not None:
            active_gate = float(g)
        if (
            active_gate is not None
            and active_gate > 0
            and meta.get("gate_enabled") is not False
        ):
            gate_enabled = True
            deduped.append(_level("gate", "GATE", active_gate, "#f472b6"))
        else:
            active_gate = None
            gate_enabled = False
    except Exception:
        active_gate = None
        gate_enabled = False

    return {
        "symbol": sym,
        "interval": interval,
        "side": side,
        "entry": entry,
        "mark": mark,
        "notional": notional,
        "unrealized_pnl": unrealized_pnl,
        "pnl_pct": pnl_pct,
        "quote_volume": quote_volume,
        "candles": candles,
        "levels": deduped,
        "gate": gates,
        "active_gate": active_gate if gate_enabled else None,
        "bot": {
            "running": bot_running,
            "direction": bot_direction,
            "gate_price": active_gate if gate_enabled else None,
            "gate_enabled": gate_enabled,
        },
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    token = _env("API_TOKEN")
    if not token:
        return False
    auth = handler.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == token
    return handler.headers.get("X-Api-Token", "").strip() == token


def _normalize_symbol(raw: str) -> str | None:
    sym = raw.strip().upper()
    if not SYM_RE.match(sym):
        return None
    return sym


def _dry_run_preview(symbol: str, direction: str | None) -> str:
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "orderbook_dca_grid.py"),
        symbol,
        "--dry-run",
        "--recv-window",
        os.getenv("RECV_WINDOW", "15000"),
    ]
    if direction:
        cmd.extend(["--direction", direction])
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip()
    if proc.returncode != 0:
        return f"❌ Dry-run failed (exit {proc.returncode}).\n{out or '(no output)'}"
    return out or "Dry-run completed (no output)."


class Handler(BaseHTTPRequestHandler):
    server_version = "OrderbookTradingAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        _json_response(self, 204, {})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/health":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "backend": botctl.detect_backend(),
                    "auth_configured": bool(_env("API_TOKEN")),
                },
            )
            return

        if not _authorized(self):
            _json_response(self, 401, {"ok": False, "error": "Unauthorized"})
            return

        try:
            if method == "GET" and path == "/scan":
                self._handle_scan(qs)
                return
            if method == "GET" and path.startswith("/scan/"):
                sym = _normalize_symbol(path[len("/scan/") :])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_scan_symbol(sym)
                return
            if method == "GET" and path == "/positions":
                self._handle_positions()
                return
            if method == "GET" and path.startswith("/chart/"):
                sym = _normalize_symbol(path[len("/chart/") :])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_chart(sym, qs)
                return
            if method == "GET" and path == "/bots":
                self._handle_bots_list()
                return
            if method == "GET" and path.startswith("/bots/") and path.count("/") == 2:
                sym = _normalize_symbol(path.split("/")[-1])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_bot_status(sym)
                return
            if method == "POST" and path.startswith("/bots/") and path.endswith("/start"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    _json_response(self, 404, {"ok": False, "error": "Not found"})
                    return
                sym = _normalize_symbol(parts[1])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_bot_start(sym)
                return
            if method == "POST" and path.startswith("/bots/") and path.endswith("/stop"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    _json_response(self, 404, {"ok": False, "error": "Not found"})
                    return
                sym = _normalize_symbol(parts[1])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_bot_stop(sym)
                return
            if method == "POST" and path.startswith("/bots/") and path.endswith("/config"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    _json_response(self, 404, {"ok": False, "error": "Not found"})
                    return
                sym = _normalize_symbol(parts[1])
                if not sym:
                    _json_response(self, 400, {"ok": False, "error": "Invalid symbol"})
                    return
                self._handle_bot_config(sym)
                return
            _json_response(self, 404, {"ok": False, "error": "Not found"})
        except ValueError as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc)})
        except urllib.error.URLError as exc:
            _json_response(self, 502, {"ok": False, "error": f"Upstream error: {exc}"})
        except subprocess.TimeoutExpired:
            _json_response(self, 504, {"ok": False, "error": "Command timed out"})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {"ok": False, "error": str(exc)})

    def _handle_scan(self, qs: dict[str, list[str]]) -> None:
        top = int((qs.get("top") or ["15"])[0])
        min_volume = float((qs.get("min_volume") or ["5000000"])[0])
        sections_raw = (qs.get("sections") or ["gainers,losers,hots"])[0]
        sections = {s.strip().lower() for s in sections_raw.split(",") if s.strip()}
        with_trend = (qs.get("with_trend") or ["0"])[0].lower() in ("1", "true", "yes")
        base = _env("FAPI_BASE", futures_scan.FAPI_BASE) or futures_scan.FAPI_BASE
        data = futures_scan.scan_sections(
            base=base,
            top=top,
            min_volume=min_volume,
            sections=sections,
            with_trend=with_trend,
        )
        _json_response(self, 200, {"ok": True, **data})

    def _handle_scan_symbol(self, sym: str) -> None:
        base = _env("FAPI_BASE", futures_scan.FAPI_BASE) or futures_scan.FAPI_BASE
        data = futures_scan.symbol_insight_json(sym, base=base)
        _json_response(self, 200, {"ok": True, "insight": data})

    def _handle_positions(self) -> None:
        positions = list_open_positions()
        total_pnl = sum(float(p["unrealized_pnl"]) for p in positions)
        total_notional = sum(float(p["notional"]) for p in positions)
        _json_response(
            self,
            200,
            {
                "ok": True,
                "positions": positions,
                "count": len(positions),
                "total_unrealized_pnl": total_pnl,
                "total_notional": total_notional,
            },
        )

    def _handle_chart(self, sym: str, qs: dict[str, list[str]]) -> None:
        interval = (qs.get("interval") or ["15m"])[0].strip() or "15m"
        limit = int((qs.get("limit") or ["120"])[0])
        data = chart_payload(sym, interval=interval, limit=limit)
        _json_response(self, 200, {"ok": True, **data})

    def _handle_bots_list(self) -> None:
        backend = botctl.detect_backend()
        running = botctl.list_running(backend)
        symbols: list[str] = []
        for item in running:
            if item.startswith("FIB:"):
                continue
            symbols.append(item.upper())
        _json_response(
            self,
            200,
            {
                "ok": True,
                "backend": backend,
                "running": symbols,
                "status_text": botctl.list_status(backend),
            },
        )

    def _handle_bot_status(self, sym: str) -> None:
        backend = botctl.detect_backend()
        running = botctl.is_running(sym, backend)
        meta = botctl.run_meta(sym)
        gate = meta.get("gate_price")
        try:
            gate_f = float(gate) if gate is not None else None
        except (TypeError, ValueError):
            gate_f = None
        if gate_f is not None and gate_f <= 0:
            gate_f = None
        if meta.get("gate_enabled") is False:
            gate_enabled = False
            gate_f = None
        else:
            # Missing flag + price ⇒ treat as enabled (legacy meta / cmdline).
            gate_enabled = gate_f is not None
        direction = meta.get("direction")
        if direction is not None:
            direction = str(direction).lower()
            if direction not in ("long", "short", "auto"):
                direction = None
        _json_response(
            self,
            200,
            {
                "ok": True,
                "symbol": sym,
                "backend": backend,
                "running": running,
                "status_text": botctl.status(sym, backend),
                "direction": direction,
                "gate_price": gate_f if gate_enabled else None,
                "gate_enabled": gate_enabled,
                "dry_run": False if running else None,
            },
        )

    def _handle_bot_config(self, sym: str) -> None:
        body = _read_json(self)
        direction = body.get("direction")
        if direction is not None:
            direction = str(direction).strip().lower()
            if direction not in ("long", "short", "auto"):
                raise ValueError("direction must be long, short, or auto")
        gate_enabled = bool(body.get("gate_enabled", False))
        gate_price = body.get("gate_price")
        if gate_price is not None and gate_price != "":
            gate_price = float(gate_price)
        else:
            gate_price = None
        restart = bool(body.get("restart_if_running", True))
        msg = botctl.apply_gate_config(
            sym,
            direction=direction,
            gate_price=gate_price,
            gate_enabled=gate_enabled,
            restart_if_running=restart,
        )
        ok = not msg.startswith("❌")
        meta = botctl.run_meta(sym)
        _json_response(
            self,
            200 if ok else 400,
            {
                "ok": ok,
                "symbol": sym,
                "message": msg,
                "running": botctl.is_running(sym),
                "direction": meta.get("direction"),
                "gate_price": meta.get("gate_price") if meta.get("gate_enabled") else None,
                "gate_enabled": bool(meta.get("gate_enabled")),
            },
        )

    def _handle_bot_start(self, sym: str) -> None:
        body = _read_json(self)
        direction = body.get("direction")
        if direction is not None:
            direction = str(direction).strip().lower()
            if direction not in ("long", "short", "auto"):
                raise ValueError("direction must be long, short, or auto")
        else:
            direction = None
        gate_price = body.get("gate_price")
        if gate_price is not None and gate_price != "":
            gate_price = float(gate_price)
        else:
            gate_price = None
        dry_run = bool(body.get("dry_run", False))

        if dry_run:
            msg = _dry_run_preview(sym, direction)
            _json_response(
                self,
                200,
                {"ok": True, "dry_run": True, "symbol": sym, "message": msg},
            )
            return

        msg = botctl.start(sym, direction=direction, gate_price=gate_price)
        ok = not msg.startswith("❌") and not msg.startswith("⛔")
        _json_response(
            self,
            200 if ok else 409,
            {
                "ok": ok,
                "dry_run": False,
                "symbol": sym,
                "running": botctl.is_running(sym),
                "message": msg,
            },
        )

    def _handle_bot_stop(self, sym: str) -> None:
        msg = botctl.stop(sym)
        _json_response(
            self,
            200,
            {
                "ok": True,
                "symbol": sym,
                "running": botctl.is_running(sym),
                "message": msg,
            },
        )


def main() -> None:
    load_env_file(None)
    token = _env("API_TOKEN")
    if not token:
        print(
            "API_TOKEN is required in .env (or environment). "
            "Add e.g. API_TOKEN=change-me-to-a-long-secret",
            file=sys.stderr,
        )
        sys.exit(1)

    host = _env("API_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(_env("API_PORT", "8787") or "8787")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Orderbook Trading API on http://{host}:{port}")
    print(f"Backend: {botctl.detect_backend()} · auth: Bearer token")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
