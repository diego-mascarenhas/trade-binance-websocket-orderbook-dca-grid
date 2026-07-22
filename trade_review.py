#!/usr/bin/env python3
"""DeepSeek situational review — compact, itemized (correction ETA / wall / SL)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from futures_scan import FAPI_BASE, build_insight, fetch_tickers, trading_usdt_perps

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
SL_LIQ_BUFFER_PCT = 4.0  # SL trigger this % before liquidation (toward mark)


def _deepseek_chat(
    api_key: str,
    *,
    system: str,
    user: str,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
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


def _correction_walls(
    bids: list[list[float]], asks: list[list[float]], mark: float, is_long: bool, n: int = 3,
) -> list[dict[str, float]]:
    """Strong book walls in the correction direction (toward recovery)."""
    levels = asks if is_long else bids
    cands: list[tuple[float, float, float]] = []
    for price, qty in levels:
        if is_long and price <= mark:
            continue
        if not is_long and price >= mark:
            continue
        dist = abs(price - mark) / mark * 100 if mark > 0 else 0.0
        cands.append((price, qty, dist))
    cands.sort(key=lambda x: x[1], reverse=True)
    picked: list[tuple[float, float, float]] = []
    for price, qty, dist in cands:
        if all(abs(price - p) / mark * 100 >= 0.5 for p, _, _ in picked):
            picked.append((price, qty, dist))
        if len(picked) >= n:
            break
    picked.sort(key=lambda x: x[2])
    return [{"price": p, "dist_pct": round(d, 2)} for p, _, d in picked]


def _sl_before_liq(liq: float, mark: float, is_long: bool) -> float | None:
    if liq <= 0 or mark <= 0:
        return None
    buf = SL_LIQ_BUFFER_PCT / 100.0
    if is_long:
        return liq * (1.0 + buf) if liq < mark else None
    return liq * (1.0 - buf) if liq > mark else None


def gather_context(symbol: str, *, recv_window: int = 15000) -> dict[str, Any]:
    from orderbook_dca_grid import (
        _detect_open_side,
        _resolve_hedge,
        count_dca_orders,
        fetch_depth,
        get_position_meta,
        get_symbol_leverage,
        liq_distance_pct,
        load_env_file,
        load_keys,
        select_walls,
        sum_dca_notional,
        _signed_request,
    )
    import orderbook_staged_exit as staged

    load_env_file(None)
    api, sec = load_keys(None)
    if not api or not sec:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_SECRET_KEY required in .env")

    sym = symbol.upper()
    base = os.getenv("FAPI_BASE", FAPI_BASE).rstrip("/")
    if sym not in trading_usdt_perps(base):
        raise RuntimeError(f"{sym} is not a USDT-M perpetual on Binance")

    ticker = fetch_tickers(base, {sym})[0]
    insight = build_insight(base, sym, ticker)
    hedge = _resolve_hedge(argparse.Namespace(position_mode="auto", recv_window=recv_window), api, sec)
    side_is_long, qty, entry = _detect_open_side(sym, hedge, api, sec, recv_window)

    position: dict[str, Any] = {"open": False}
    liq_price: float | None = None
    mark = ticker.last

    if side_is_long is not None and qty > 0:
        is_long = side_is_long
        meta = get_position_meta(sym, is_long, hedge, api, sec, recv_window)
        if meta.get("entry"):
            entry = float(meta["entry"])
        lev = int(meta.get("leverage") or 0) or get_symbol_leverage(sym, api, sec, recv_window)
        pnl = float(meta.get("unrealized_pnl", 0) or 0)
        notional = float(meta.get("notional", 0) or 0)
        roi = (pnl / (notional / lev) * 100.0) if lev > 0 and notional > 0 else None

        rows = _signed_request("GET", "/fapi/v2/positionRisk", {"symbol": sym}, api, sec, recv_window)
        for r in rows if isinstance(rows, list) else []:
            if abs(float(r.get("positionAmt", 0) or 0)) <= 0:
                continue
            mark = float(r.get("markPrice", 0) or ticker.last)
            liq_price = float(r.get("liquidationPrice", 0) or 0) or None
            liq_dist = liq_distance_pct(float(r.get("positionAmt", 0)), mark, liq_price or 0)
            break
        else:
            liq_dist = None

        move_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)

        position = {
            "open": True,
            "direction": "LONG" if is_long else "SHORT",
            "qty": qty,
            "entry": entry,
            "mark": mark,
            "move_pct_from_entry": round(move_pct, 2),
            "leverage": lev,
            "unrealized_pnl_usdt": round(pnl, 2),
            "roi_pct_on_margin": round(roi, 1) if roi is not None else None,
            "liquidation_price": liq_price,
            "liq_distance_pct": round(liq_dist, 1) if liq_dist is not None else None,
            "sl_before_liq_price": round(_sl_before_liq(liq_price or 0, mark, is_long) or 0, 6) or None,
        }

    depth = fetch_depth(sym, 100)
    bids = [[float(p), float(q)] for p, q in depth["bids"]]
    asks = [[float(p), float(q)] for p, q in depth["asks"]]

    levels_plan: dict[str, Any] = {}
    if position.get("open"):
        is_long = position["direction"] == "LONG"
        dca_levels = bids if is_long else asks
        dca_walls = select_walls(dca_levels, mark, is_long, 5, 0.8, 0.1, 12.0)
        corr_walls = _correction_walls(bids, asks, mark, is_long)
        levels_plan = {
            "far_dca_wall_price": dca_walls[-1][0] if dca_walls else None,
            "near_dca_wall_price": dca_walls[0][0] if dca_walls else None,
            "correction_wall_prices": [w["price"] for w in corr_walls],
            "correction_walls": corr_walls,
        }

    oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, recv_window) or []
    state = staged.load_state(sym)

    return {
        "symbol": sym,
        "market": {
            "last": mark,
            "change_24h_pct": round(ticker.change_pct, 1),
            "momentum_1h_pct": round(insight.change_1h, 1),
            "momentum_4h_pct": round(insight.change_4h, 1),
            "trend": insight.trend,
        },
        "position": position,
        "levels": levels_plan,
        "bot": {
            "staged_phase": str(state.get("phase", "idle")),
            "open_dca_limits": count_dca_orders(oo, sym),
            "tp1_price": state.get("tp1_price"),
            "be_price": state.get("be_price"),
        },
    }


def deepseek_review(context: dict[str, Any], api_key: str, *, model: str, base_url: str) -> dict[str, Any]:
    system = (
        "Crypto futures analyst. Reply in English only. "
        "User prefers waiting for correction when underwater. "
        "Use ONLY numbers from context (sl_before_liq_price, far_dca_wall_price, correction_wall_prices). "
        "Be VERY concise. JSON only:\n"
        "{\n"
        '  "verdict": "wait|far_wall|sl_now|cut|trust_bot",\n'
        '  "correction_eta": "2-6h|6-24h|1-3d|3-7d|unlikely|n/a",\n'
        '  "correction_target_price": number|null,\n'
        '  "items": [{"icon":"emoji","text":"max 10 words"}, ... max 5 items],\n'
        '  "use_far_wall": true|false,\n'
        '  "use_sl_before_liq": true|false,\n'
        '  "confidence": 0.0-1.0\n'
        "}\n"
        "items must cover: situation, correction timing, action (wait/wall/SL), bot, risk if any. "
        "Pick correction_target_price from correction_wall_prices. "
        "If liq_distance_pct < 15 set use_sl_before_liq true. "
        "All item text MUST be in English. Not financial advice."
    )
    user = json.dumps(context, separators=(",", ":"))
    return _deepseek_chat(api_key, system=system, user=user, model=model, base_url=base_url)


def _px(x: float | None) -> str:
    if x is None or x == 0:
        return "—"
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6f}"


def format_compact(ctx: dict[str, Any], review: dict[str, Any] | None = None) -> str:
    sym = ctx["symbol"]
    m = ctx["market"]
    lines: list[str] = [f"🔍 {sym} · {m['trend']} · 24h {m['change_24h_pct']:+.1f}%"]

    p = ctx["position"]
    if p.get("open"):
        lines.append(
            f"{'🍏' if p['direction'] == 'LONG' else '🍎'} {p['direction']} "
            f"{p['qty']:g} @ {_px(p['entry'])} → mark {_px(p['mark'])} "
            f"({p['move_pct_from_entry']:+.1f}%) · {p['leverage']}x"
        )
        pnl_e = "🟢" if p["unrealized_pnl_usdt"] >= 0 else "🔴"
        roi = f" · {p['roi_pct_on_margin']:+.0f}% ROI" if p.get("roi_pct_on_margin") is not None else ""
        liq = f" · liq {_px(p.get('liquidation_price'))} ({p['liq_distance_pct']:.0f}%)" if p.get("liq_distance_pct") else ""
        lines.append(f"{pnl_e} PnL {p['unrealized_pnl_usdt']:+.2f} USDT{roi}{liq}")
    else:
        lines.append("⚪ No open position")
        return "\n".join(lines)

    lv = ctx.get("levels") or {}
    sl_px = p.get("sl_before_liq_price")
    far_px = lv.get("far_dca_wall_price")

    if review:
        eta = review.get("correction_eta", "n/a")
        lines.append(f"⏱ Correction: {eta}")
        tgt = review.get("correction_target_price")
        if tgt:
            lines.append(f"🎯 Target ~{_px(float(tgt))}")
        verdict = str(review.get("verdict", "")).lower()
        vmap = {
            "wait": "✅ Wait for correction",
            "far_wall": "🧱 Consider far-wall DCA limit",
            "sl_now": "🛡 Set SL before liquidation",
            "cut": "✂️ Close manually",
            "trust_bot": "🤖 Trust staged exit",
        }
        lines.append(vmap.get(verdict, f"📌 {verdict}"))

        for item in (review.get("items") or [])[:5]:
            icon = str(item.get("icon", "•"))[:4]
            text = str(item.get("text", ""))[:80]
            if text:
                lines.append(f"{icon} {text}")

        if review.get("use_far_wall") and far_px:
            lines.append(f"🧱 Far DCA wall: {_px(far_px)}")
        if review.get("use_sl_before_liq") and sl_px:
            lines.append(f"🛡 Suggested SL (pre-liq): {_px(sl_px)}")

        conf = review.get("confidence")
        if conf is not None:
            lines.append(f"📊 Confidence {float(conf):.0%}")
    else:
        if lv.get("correction_wall_prices"):
            lines.append(f"🎯 Correction walls: {', '.join(_px(x) for x in lv['correction_wall_prices'][:3])}")
        if far_px:
            lines.append(f"🧱 Far DCA wall: {_px(far_px)}")
        if sl_px:
            lines.append(f"🛡 Pre-liq SL: {_px(sl_px)}")

    b = ctx["bot"]
    lines.append(f"🤖 Bot: {b['staged_phase']} · DCA {b['open_dca_limits']} limits")
    return "\n".join(lines)


def review_symbol(
    symbol: str,
    *,
    skip_ai: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    recv_window: int = 15000,
) -> str:
    ctx = gather_context(symbol, recv_window=recv_window)
    if skip_ai:
        return format_compact(ctx, None)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return format_compact(ctx, None) + "\n⚠️ DEEPSEEK_API_KEY not set"

    try:
        review = deepseek_review(
            ctx,
            api_key,
            model=model or os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL),
            base_url=base_url or os.getenv("DEEPSEEK_BASE", DEEPSEEK_DEFAULT_BASE),
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return format_compact(ctx, None) + f"\n⚠️ DeepSeek: {exc}"

    return format_compact(ctx, review)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compact DeepSeek review for an open futures position.")
    p.add_argument("symbol", help="Symbol, e.g. NEARUSDT")
    p.add_argument("--context-only", action="store_true", help="Skip DeepSeek")
    p.add_argument("--json", action="store_true", help="Raw JSON output")
    p.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL))
    p.add_argument("--deepseek-base", default=os.getenv("DEEPSEEK_BASE", DEEPSEEK_DEFAULT_BASE))
    p.add_argument("--recv-window", type=int, default=int(os.getenv("RECV_WINDOW", "15000")))
    p.add_argument("--env-file", default=None)
    return p.parse_args()


def main() -> int:
    from orderbook_dca_grid import load_env_file

    args = parse_args()
    load_env_file(args.env_file)
    sym = args.symbol.upper()

    if args.json:
        ctx = gather_context(sym, recv_window=args.recv_window)
        payload: dict[str, Any] = {"context": ctx}
        if not args.context_only and os.getenv("DEEPSEEK_API_KEY", "").strip():
            try:
                payload["review"] = deepseek_review(
                    ctx, os.getenv("DEEPSEEK_API_KEY", "").strip(),
                    model=args.model, base_url=args.deepseek_base,
                )
            except Exception as exc:
                payload["review_error"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 0

    print(review_symbol(sym, skip_ai=args.context_only, model=args.model,
                        base_url=args.deepseek_base, recv_window=args.recv_window))
    return 0


if __name__ == "__main__":
    sys.exit(main())
