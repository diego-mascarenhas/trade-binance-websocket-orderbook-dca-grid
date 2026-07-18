"""Telegram alerts for DCA grid + staged exit (send-only, same env as dashboard bot)."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _send_sync(text: str) -> bool:
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    body: dict[str, Any] = {"chat_id": _chat_id(), "text": text}
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def _send_async(text: str) -> None:
    if not is_configured():
        return
    threading.Thread(
        target=_send_sync, args=(text,), daemon=True, name="telegram-send",
    ).start()


def _dir_emoji(direction: str) -> str:
    return "🍏" if direction.upper() == "LONG" else "🍎"


def fmt_usdt(qty: float, price: float) -> str:
    """Position notional in USDT (qty × price)."""
    return f"{abs(qty) * abs(price):,.2f}"


def fmt_vol_usdt(notional: float, leverage: float | int | None = None) -> str:
    """Format notional USDT; append leverage when known (futures exposure)."""
    label = f"Vol: {abs(notional):,.2f} USDT"
    if leverage and float(leverage) > 0:
        label += f" · {int(leverage)}x"
    return label


def fmt_vol(qty: float, price: float, leverage: float | int | None = None) -> str:
    return fmt_vol_usdt(abs(qty) * abs(price), leverage)


def send_bot(message: str) -> None:
    _send_async(f"🤖 {message}")


def send_position(direction: str, message: str) -> None:
    _send_async(f"{_dir_emoji(direction)} {message}")


def send_tp(message: str) -> None:
    _send_async(f"🥳 {message}")


def send_trailing(message: str) -> None:
    _send_async(f"🏄 {message}")


def send_shield(message: str) -> None:
    _send_async(f"🛡️ {message}")


def send_warn(message: str) -> None:
    _send_async(f"⚠️ {message}")


def send_grid(message: str) -> None:
    _send_async(f"🧱 {message}")


def fmt_pnl(pnl_usdt: float, notional: float, leverage: float | int | None = None) -> str:
    """Standard PnL line: PnL: +X.XX USDT (+Y.YY% ROI)."""
    lev = float(leverage) if leverage else 0.0
    margin = notional / lev if lev > 0 and notional > 0 else 0.0
    roi = (pnl_usdt / margin * 100.0) if margin > 0 else None
    line = f"PnL: {pnl_usdt:+,.2f} USDT"
    if roi is not None:
        line += f" ({roi:+.2f}% ROI)"
    return line


def pnl_suffix(
    pnl_usdt: float | None,
    notional: float,
    leverage: float | int | None = None,
) -> str:
    """Newline-prefixed PnL line, or empty if unknown."""
    if pnl_usdt is None:
        return ""
    return f"\n{fmt_pnl(pnl_usdt, notional, leverage)}"


def _close_emoji(pnl_usdt: float | None) -> str:
    if pnl_usdt is None:
        return "🤖"
    if pnl_usdt > 0:
        return "🥳"
    if pnl_usdt < 0:
        return "😢"
    return "🤖"


def notify_dca_filled(
    symbol: str,
    direction: str,
    fill_qty: float,
    fill_price: float,
    pos_qty: float,
    entry: float,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(pos_qty) * abs(entry)
    fill_vol = abs(fill_qty) * abs(fill_price)
    send_position(
        direction,
        f"{symbol.upper()} futures\n#DCA {direction.upper()}\n"
        f"+{fill_qty:g} @ {fill_price:g} · Vol: {fill_vol:,.2f} USDT\n"
        f"Position {pos_qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_supervise_started(symbol: str, exit_mode: str) -> None:
    send_bot(f"{symbol.upper()} DCA supervise started\nExit: {exit_mode}")


def notify_grid_armed(
    symbol: str,
    direction: str,
    order_count: int,
    *,
    dca_only: bool = False,
    grid_vol_usdt: float | None = None,
    leverage: float | int | None = None,
) -> None:
    kind = "DCA-only re-arm" if dca_only else "Grid armed"
    vol_line = ""
    if grid_vol_usdt and grid_vol_usdt > 0:
        vol_line = f"\n{fmt_vol_usdt(grid_vol_usdt, leverage)}"
    send_grid(
        f"{symbol.upper()} futures\n{kind} · {order_count} limit(s) · {direction.upper()}{vol_line}",
    )


def notify_position_open(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(qty) * abs(entry)
    send_position(
        direction,
        f"{symbol.upper()} futures\n#OPEN {direction.upper()}\n"
        f"Qty {qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_orphan_recovery(
    symbol: str,
    direction: str,
    qty: float,
    entry: float = 0.0,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else (abs(qty) * abs(entry) if entry > 0 else 0)
    vol = f" · {fmt_vol_usdt(notional, leverage)}" if notional > 0 else ""
    send_warn(
        f"{symbol.upper()} futures\nEntry filled (no orphan cancel)\n"
        f"DCA-only re-arm · {direction.upper()} qty {qty:g}{vol}"
        f"{pnl_suffix(pnl_usdt, notional, leverage) if notional > 0 else ''}",
    )


def notify_supervisor_error(symbol: str, detail: str) -> None:
    text = (detail or "")[:420]
    send_warn(f"{symbol.upper()} DCA supervisor error\n{text}")


def notify_staged_armed(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    tp1_price: float,
    tp1_pct: float,
    *,
    tp1_qty: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    tp1_q = tp1_qty if tp1_qty is not None else qty * 0.7
    notional = abs(qty) * abs(entry)
    send_position(
        direction,
        f"{symbol.upper()} futures · staged exit\n"
        f"TP1 {tp1_pct:g}% @ {tp1_price:g} · {fmt_vol(tp1_q, tp1_price, leverage)}\n"
        f"Position {qty:g} @ {entry:g} · {fmt_vol(qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_tp1_filled(
    symbol: str,
    direction: str,
    tp1_qty: float,
    remain_qty: float,
    entry: float,
    *,
    tp1_price: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    price = tp1_price if tp1_price and tp1_price > 0 else entry
    notional = abs(remain_qty) * abs(entry)
    send_tp(
        f"{symbol.upper()} futures\nTP1 filled · {direction.upper()}\n"
        f"Closed {tp1_qty:g} · {fmt_vol(tp1_qty, price, leverage)}\n"
        f"Runner {remain_qty:g} · {fmt_vol(remain_qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_profit_lock_sl(
    symbol: str,
    direction: str,
    runner_qty: float,
    entry: float,
    sl_price: float,
    *,
    closed_pct: float = 70.0,
    runner_pct: float | None = None,
    trigger: str = "tp1_partial",
    closed_qty: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    """Profit lock after partial TP — shield icon (matches dashboard format)."""
    run_pct = runner_pct if runner_pct is not None else max(0.0, 100.0 - closed_pct)
    notional = abs(runner_qty) * abs(entry)
    closed_vol = ""
    if closed_qty is not None and closed_qty > 0:
        closed_vol = f" · {fmt_vol(closed_qty, entry, leverage)}"
    send_shield(
        f"{symbol.upper()} futures\n"
        f"PROFIT LOCK SL · {direction.upper()}\n"
        f"Trigger: {trigger}\n"
        f"~{closed_pct:.0f}% closed{closed_vol} · runner {run_pct:.0f}% · {fmt_vol(runner_qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}\n"
        f"SL → {sl_price:g}"
    )


def notify_position_closed(
    symbol: str,
    direction: str,
    *,
    after_runner: bool = False,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    vol = f" · {fmt_vol_usdt(vol_usdt, leverage)}" if vol_usdt and vol_usdt > 0 else ""
    emoji = _close_emoji(pnl_usdt)
    pnl_line = pnl_suffix(pnl_usdt, vol_usdt or 0.0, leverage) if pnl_usdt is not None else ""
    _send_async(f"{emoji} {symbol.upper()} futures\n#CLOSED {direction.upper()}{vol}{pnl_line}")


def notify_sl_at_entry(symbol: str, direction: str, qty: float, entry: float) -> None:
    notify_profit_lock_sl(symbol, direction, qty, entry, entry)


def notify_trail_started(
    symbol: str,
    direction: str,
    qty: float,
    activate: float,
    callback: float,
    *,
    entry: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    ref = entry if entry and entry > 0 else activate
    notional = abs(qty) * abs(ref)
    send_trailing(
        f"{symbol.upper()} futures\nTrailing runner · {direction.upper()}\n"
        f"Qty {qty:g} · {fmt_vol(qty, ref, leverage)}\n"
        f"Activate {activate:g} · callback {callback:g}%"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


# ── Fib / micro-grid ─────────────────────────────────────────────────────────

def notify_fib_started(symbol: str, *, direction: str = "auto", note: str = "") -> None:
    extra = f"\n{note}" if note else ""
    send_bot(f"{symbol.upper()} FIB micro-grid started\nDir: {direction.upper()}{extra}")


def notify_fib_grid_armed(
    symbol: str,
    direction: str,
    levels: int,
    *,
    wait_pullback: bool = True,
    grid_vol_usdt: float | None = None,
    mark: float | None = None,
    leverage: float | int | None = None,
) -> None:
    kind = "LIMIT pullback" if wait_pullback else "MARKET + grid"
    vol_line = ""
    if grid_vol_usdt and grid_vol_usdt > 0:
        vol_line = f"\n{fmt_vol_usdt(grid_vol_usdt, leverage)}"
    mark_line = f"\nMark {mark:g}" if mark and mark > 0 else ""
    send_grid(
        f"{symbol.upper()} futures\n#FIB Grid armed · {direction.upper()}\n"
        f"{kind} · {levels} level(s){mark_line}{vol_line}",
    )


def notify_fib_open(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    *,
    tp: float | None = None,
    sl: float | None = None,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(qty) * abs(entry)
    exits = ""
    if tp and tp > 0:
        exits += f"\nTP {tp:g}"
    if sl and sl > 0:
        exits += f" · SL {sl:g}"
    send_position(
        direction,
        f"{symbol.upper()} futures\n#FIB OPEN {direction.upper()}\n"
        f"Qty {qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{exits}{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_fib_fill(
    symbol: str,
    direction: str,
    fill_qty: float,
    fill_price: float,
    pos_qty: float,
    entry: float,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(pos_qty) * abs(entry)
    fill_vol = abs(fill_qty) * abs(fill_price)
    send_position(
        direction,
        f"{symbol.upper()} futures\n#FIB FILL {direction.upper()}\n"
        f"+{fill_qty:g} @ {fill_price:g} · Vol: {fill_vol:,.2f} USDT\n"
        f"Position {pos_qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_fib_protect_trail(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    callback: float,
    *,
    profit_pct: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = abs(qty) * abs(entry)
    profit = f"\nProfit {profit_pct:+.2f}%" if profit_pct is not None else ""
    send_trailing(
        f"{symbol.upper()} futures\n#FIB TRAIL {direction.upper()}\n"
        f"Full grid filled · callback {callback:g}%{profit}\n"
        f"Qty {qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_fib_disarm(symbol: str, direction: str, reason: str) -> None:
    send_warn(
        f"{symbol.upper()} futures\n#FIB DISARM {direction.upper()}\n"
        f"Reason: {reason}",
    )


def notify_fib_adopt(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
    trail: bool = False,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(qty) * abs(entry)
    trail_note = " · trail on" if trail else ""
    send_bot(
        f"{symbol.upper()} futures\n#FIB ADOPT {direction.upper()}{trail_note}\n"
        f"Qty {qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_fib_closed(
    symbol: str,
    direction: str,
    *,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    vol = f" · {fmt_vol_usdt(vol_usdt, leverage)}" if vol_usdt and vol_usdt > 0 else ""
    emoji = _close_emoji(pnl_usdt)
    pnl_line = pnl_suffix(pnl_usdt, vol_usdt or 0.0, leverage) if pnl_usdt is not None else ""
    _send_async(f"{emoji} {symbol.upper()} futures\n#FIB CLOSED {direction.upper()}{vol}{pnl_line}")


def notify_fib_error(symbol: str, detail: str) -> None:
    text = (detail or "")[:420]
    send_warn(f"{symbol.upper()} FIB error\n{text}")
