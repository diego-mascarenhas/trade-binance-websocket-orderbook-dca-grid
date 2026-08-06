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


def _public_chat_id() -> str:
    try:
        import pumpstall_telegram as pst

        return pst.chat_id()
    except Exception:
        return os.getenv("TELEGRAM_PUMPSTALL_CHAT_ID", "").strip()


def _ops_is_public_channel() -> bool:
    """True when TELEGRAM_CHAT_ID points at the public Pumpstall channel."""
    ops = _chat_id()
    pub = _public_chat_id()
    return bool(ops) and bool(pub) and ops == pub


def _ops_trade_alerts() -> bool:
    """Mirror size-bearing trade alerts to TELEGRAM_CHAT_ID.

    Default **off** when ``TELEGRAM_PUMPSTALL_CHAT_ID`` is a separate public
    channel — private chat stays for botctl (/pump, /report) and bank notices.
    Set ``TELEGRAM_OPS_TRADE_ALERTS=1`` to also get detailed Vol/qty privately.
    """
    raw = os.getenv("TELEGRAM_OPS_TRADE_ALERTS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    pub = _public_chat_id()
    ops = _chat_id()
    if pub and ops and str(pub) != str(ops):
        return False
    return True


def _skip_ops_trade() -> bool:
    """True → do not send detailed trade alert to private ops."""
    return _ops_is_public_channel() or not is_configured() or not _ops_trade_alerts()


def _pnl_pct_public(
    pnl_usdt: float | None,
    notional: float | None,
    leverage: float | int | None = None,
    *,
    entry: float | None = None,
    mark: float | None = None,
    direction: str | None = None,
) -> float | None:
    try:
        import pumpstall_telegram as pst

        return pst.pnl_pct_from_close(
            pnl_usdt,
            notional,
            leverage,
            entry=entry,
            mark=mark,
            direction=direction,
        )
    except Exception:
        return None


def _send_public_html(text: str) -> bool:
    try:
        import pumpstall_telegram as pst

        return pst._send_html(text)  # noqa: SLF001
    except Exception as exc:
        logger.warning("Public channel send failed: %s", exc)
        return False


def _looks_like_size_leak(text: str) -> bool:
    t = text.lower()
    return (
        "vol:" in t
        or " usdt" in t
        or "qty " in t
        or "position " in t and "@" in t
    )


def _send_sync(text: str) -> bool:
    if not is_configured():
        return False
    # Safety: never post size/volume to the public Pumpstall channel
    if _ops_is_public_channel() and _looks_like_size_leak(text):
        logger.warning("Blocked Telegram size leak to public channel")
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


def fmt_pnl(
    pnl_usdt: float,
    notional: float,
    leverage: float | int | None = None,
    *,
    entry: float | None = None,
    mark: float | None = None,
    direction: str | None = None,
) -> str:
    """Standard PnL line: PnL: +X.XX USDT (+Y.YY%). %% from avg entry when possible."""
    pct: float | None = None
    try:
        import pumpstall_telegram as pst

        pct = pst.pnl_pct_from_close(
            pnl_usdt,
            notional,
            leverage,
            entry=entry,
            mark=mark,
            direction=direction,
        )
    except Exception:
        notion = abs(float(notional or 0))
        if notion > 0:
            pct = pnl_usdt / notion * 100.0
    line = f"PnL: {pnl_usdt:+,.2f} USDT"
    if pct is not None:
        line += f" ({pct:+.2f}%)"
    return line


def pnl_suffix(
    pnl_usdt: float | None,
    notional: float,
    leverage: float | int | None = None,
    *,
    entry: float | None = None,
    mark: float | None = None,
    direction: str | None = None,
) -> str:
    """Newline-prefixed PnL line, or empty if unknown."""
    if pnl_usdt is None:
        return ""
    return (
        f"\n{fmt_pnl(pnl_usdt, notional, leverage, entry=entry, mark=mark, direction=direction)}"
    )


def _close_emoji(pnl_usdt: float | None = None) -> str:
    """#CLOSE: 🥳 win · 😢 loss · 🤖 flat/unknown."""
    if pnl_usdt is None:
        return "🤖"
    if float(pnl_usdt) < 0:
        return "😢"
    if float(pnl_usdt) > 0:
        return "🥳"
    return "🤖"


def _sl_emoji(pnl_usdt: float | None) -> str:
    """#SL: 😢 when losing, else 🛡️ (protect)."""
    if pnl_usdt is not None and float(pnl_usdt) < 0:
        return "😢"
    return "🛡️"


def _tag_emoji(tag: str, direction: str, pnl_usdt: float | None = None) -> str:
    t = tag.strip().upper().lstrip("#")
    if t == "CLOSE":
        return _close_emoji(pnl_usdt)
    if t == "TP":
        return "🥳"
    if t == "BE":
        return "🛡️"
    if t == "TRAIL":
        return "🏄"
    if t == "SL":
        return _sl_emoji(pnl_usdt)
    if t in ("OPEN", "DCA"):
        return _dir_emoji(direction)
    return _dir_emoji(direction)


def _post_public_tag(
    tag: str,
    symbol: str,
    direction: str,
    *,
    pnl_usdt: float | None = None,
    notional: float | None = None,
    leverage: float | int | None = None,
    entry: float | None = None,
    mark: float | None = None,
) -> None:
    """Compact Pumpstall public alert — hashtag + %% only, never size."""
    if not _public_chat_id():
        return
    tag_u = tag.strip().upper().lstrip("#")
    emoji = _tag_emoji(tag_u, direction, pnl_usdt)
    pct = _pnl_pct_public(
        pnl_usdt,
        notional,
        leverage,
        entry=entry,
        mark=mark,
        direction=direction,
    )
    lines = [
        f"{emoji} <b>#{tag_u} {direction.upper()}</b> · <b>{symbol.upper()}</b>",
    ]
    if pct is not None:
        lines.append(f"PnL · <b>{pct:+.2f}%</b>")
    _send_public_html("\n".join(lines))


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
    mark: float | None = None,
) -> None:
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(pos_qty) * abs(entry)
    mark_px = mark if mark and mark > 0 else fill_price
    _post_public_tag(
        "DCA", symbol, direction,
        pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
        entry=entry, mark=mark_px,
    )
    if _skip_ops_trade():
        return
    fill_vol = abs(fill_qty) * abs(fill_price)
    send_position(
        direction,
        f"{symbol.upper()} futures #DCA {direction.upper()}\n"
        f"+{fill_qty:g} @ {fill_price:g} · Vol: {fill_vol:,.2f} USDT\n"
        f"Position {pos_qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage, entry=entry, mark=mark_px, direction=direction)}",
    )


def notify_supervise_started(symbol: str, exit_mode: str) -> None:
    if _skip_ops_trade():
        return
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
    # Public #OPEN when orders are placed (🍎 SHORT / 🍏 LONG). Skip re-arms.
    if not dca_only:
        _post_public_tag(
            "OPEN", symbol, direction,
            notional=grid_vol_usdt, leverage=leverage,
        )
    if _skip_ops_trade():
        return
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
    """Private ops detail on fill. Public #OPEN already sent when grid orders were placed."""
    notional = vol_usdt if vol_usdt and vol_usdt > 0 else abs(qty) * abs(entry)
    if _skip_ops_trade():
        return
    send_position(
        direction,
        f"{symbol.upper()} futures #OPEN {direction.upper()}\n"
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
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
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
    _post_public_tag(
        "TP", symbol, direction,
        pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
        entry=entry, mark=price,
    )
    if _skip_ops_trade():
        return
    send_tp(
        f"{symbol.upper()} futures #TP {direction.upper()}\n"
        f"TP1 filled\n"
        f"Closed {tp1_qty:g} · {fmt_vol(tp1_qty, price, leverage)}\n"
        f"Runner {remain_qty:g} · {fmt_vol(remain_qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_risk_reduce_armed(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    *,
    impulse_high: float,
    partial_sl: float,
    full_sl: float | None,
    reduce_pct: float,
    reduce_buffer_pct: float,
    full_buffer_pct: float,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
    swing_bars: int = 120,
    grid_top: float | None = None,
    prior_swing: float | None = None,
    full_source: str | None = None,
) -> None:
    """Announce partial cut + suggested full SL (Telegram suggests the far stop)."""
    notional = abs(qty) * abs(entry)
    if full_sl and full_sl > 0:
        if full_source == "prior_swing" and prior_swing and prior_swing > 0:
            full_line = (
                f"Suggested full SL → {full_sl:g} "
                f"(prior HTF swing {prior_swing:g} +{reduce_buffer_pct:g}%)"
            )
        else:
            full_line = (
                f"Suggested full SL → {full_sl:g} "
                f"(fallback +{full_buffer_pct:g}% over RR swing {impulse_high:g})"
            )
    else:
        full_line = "Full SL: off"
    try:
        if _public_chat_id():
            _post_public_tag(
                "RISK", symbol, direction,
                pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
                entry=entry, mark=full_sl if full_sl else partial_sl,
            )
    except Exception:
        pass
    # Always suggest levels on the private botctl chat (even if trade-size alerts are off)
    if not is_configured() or _ops_is_public_channel():
        return
    detail = ""
    if not _skip_ops_trade():
        detail = (
            f" · {fmt_vol(qty, entry, leverage)}"
            f"{pnl_suffix(pnl_usdt, notional, leverage)}"
        )
    grid_note = ""
    if grid_top and grid_top > 0:
        floor = grid_top * (1.0 + reduce_buffer_pct / 100.0)
        if partial_sl + 1e-12 >= floor:
            grid_note = f"\nAbove DCA grid top {grid_top:g}"
    send_shield(
        f"{symbol.upper()} futures #RISK {direction.upper()}\n"
        f"Risk-reduce armed\n"
        f"HTF swing ({swing_bars:d}d) {impulse_high:g}\n"
        f"Cut ~{reduce_pct:.0f}% @ {partial_sl:g} (+{reduce_buffer_pct:g}%){detail}"
        f"{grid_note}\n"
        f"{full_line}"
    )


def notify_risk_reduce_filled(
    symbol: str,
    direction: str,
    *,
    closed_qty: float,
    remain_qty: float,
    entry: float,
    trigger: float,
    impulse_high: float,
    full_sl: float | None,
    rearm: bool,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
) -> None:
    notional = abs(remain_qty) * abs(entry)
    try:
        _post_public_tag(
            "RR", symbol, direction,
            pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
            entry=entry, mark=trigger if trigger > 0 else entry,
        )
    except Exception:
        pass
    rearm_txt = "re-arm DCA (still ★)" if rearm else "no DCA re-arm (not ★)"
    full_txt = f"\nFull SL still @ {full_sl:g}" if full_sl and full_sl > 0 else ""
    body = (
        f"{symbol.upper()} futures #RR {direction.upper()}\n"
        f"Risk-reduce filled @ {trigger:g}\n"
        f"Closed {closed_qty:g} · runner {remain_qty:g} · "
        f"{fmt_vol(remain_qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}\n"
        f"HTF swing was {impulse_high:g} · {rearm_txt}{full_txt}"
    )
    if _skip_ops_trade():
        send_tp(body)
        return
    send_tp(body)


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
    hashtag: str = "#SL",
) -> None:
    """Profit lock / BE protect. ``hashtag`` is #BE or #SL (#SL loss → 😢)."""
    tag = (hashtag or "#SL").strip()
    if not tag.startswith("#"):
        tag = f"#{tag}"
    tag_u = tag.upper().lstrip("#")
    run_pct = runner_pct if runner_pct is not None else max(0.0, 100.0 - closed_pct)
    notional = abs(runner_qty) * abs(entry)
    _post_public_tag(
        tag_u, symbol, direction,
        pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
        entry=entry, mark=sl_price if sl_price > 0 else entry,
    )
    if _skip_ops_trade():
        return
    closed_vol = ""
    if closed_qty is not None and closed_qty > 0:
        closed_vol = f" · {fmt_vol(closed_qty, entry, leverage)}"
    label = "BE protect" if tag_u == "BE" else "PROFIT LOCK SL"
    emoji = _tag_emoji(tag_u, direction, pnl_usdt)
    body = (
        f"{symbol.upper()} futures {tag} {direction.upper()}\n"
        f"{label}\n"
        f"Trigger: {trigger}\n"
        f"~{closed_pct:.0f}% closed{closed_vol} · runner {run_pct:.0f}% · "
        f"{fmt_vol(runner_qty, entry, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}\n"
        f"SL → {sl_price:g}"
    )
    if tag_u == "SL" and pnl_usdt is not None and float(pnl_usdt) < 0:
        _send_async(f"{emoji} {body}")
    else:
        send_shield(body)


def notify_position_closed(
    symbol: str,
    direction: str,
    *,
    after_runner: bool = False,
    vol_usdt: float | None = None,
    leverage: float | int | None = None,
    pnl_usdt: float | None = None,
    entry: float | None = None,
    mark: float | None = None,
    reason: str | None = None,
    exit_mode: str | None = None,
) -> None:
    why = (reason or "").strip()
    if not why and after_runner:
        why = "runner"

    public_chat = ""
    try:
        import pumpstall_telegram as pst

        public_chat = pst.chat_id()
    except Exception:
        pst = None  # type: ignore[assignment]

    ops_chat = _chat_id()
    # Never post volume/USDT size to the public Pumpstall channel.
    same_as_public = bool(public_chat) and public_chat == ops_chat

    if not same_as_public and _ops_trade_alerts() and is_configured():
        vol = f" · {fmt_vol_usdt(vol_usdt, leverage)}" if vol_usdt and vol_usdt > 0 else ""
        emoji = _close_emoji(pnl_usdt)
        pnl_line = (
            pnl_suffix(
                pnl_usdt,
                vol_usdt or 0.0,
                leverage,
                entry=entry,
                mark=mark,
                direction=direction,
            )
            if pnl_usdt is not None
            else ""
        )
        reason_line = f"\nReason: {why}" if why else ""
        _send_async(
            f"{emoji} {symbol.upper()} futures #CLOSE {direction.upper()}"
            f"{vol}{pnl_line}{reason_line}"
        )

    if pst is not None and public_chat:
        try:
            pst.notify_close(
                symbol,
                direction,
                pnl_usdt=pnl_usdt,
                notional=vol_usdt,
                leverage=leverage,
                entry=entry,
                mark=mark,
                reason=why or None,
                exit_mode=exit_mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pumpstall channel close notify failed: %s", exc)


def notify_sl_at_entry(symbol: str, direction: str, qty: float, entry: float) -> None:
    notify_profit_lock_sl(symbol, direction, qty, entry, entry, hashtag="#BE")


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
    _post_public_tag(
        "TRAIL", symbol, direction,
        pnl_usdt=pnl_usdt, notional=notional, leverage=leverage,
        entry=entry if entry and entry > 0 else None,
        mark=activate,
    )
    if _skip_ops_trade():
        return
    send_trailing(
        f"{symbol.upper()} futures #TRAIL {direction.upper()}\n"
        f"Trailing runner\n"
        f"Qty {qty:g} · {fmt_vol(qty, ref, leverage)}\n"
        f"Activate {activate:g} · callback {callback:g}%"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


# ── Fib / micro-grid ─────────────────────────────────────────────────────────

def notify_fib_started(symbol: str, *, direction: str = "auto", note: str = "") -> None:
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
    notional = abs(qty) * abs(entry)
    profit = f"\nProfit {profit_pct:+.2f}%" if profit_pct is not None else ""
    send_trailing(
        f"{symbol.upper()} futures\n#FIB TRAIL {direction.upper()}\n"
        f"Full grid filled · callback {callback:g}%{profit}\n"
        f"Qty {qty:g} @ {entry:g} · {fmt_vol_usdt(notional, leverage)}"
        f"{pnl_suffix(pnl_usdt, notional, leverage)}",
    )


def notify_fib_disarm(symbol: str, direction: str, reason: str) -> None:
    if _skip_ops_trade():
        return
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
    if _skip_ops_trade():
        return
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
    reason: str | None = None,
) -> None:
    if _skip_ops_trade():
        return
    vol = f" · {fmt_vol_usdt(vol_usdt, leverage)}" if vol_usdt and vol_usdt > 0 else ""
    emoji = _close_emoji(pnl_usdt)
    pnl_line = pnl_suffix(pnl_usdt, vol_usdt or 0.0, leverage) if pnl_usdt is not None else ""
    why = (reason or "").strip()
    reason_line = f"\nReason: {why}" if why else ""
    _send_async(
        f"{emoji} {symbol.upper()} futures\n"
        f"#FIB CLOSED {direction.upper()}{vol}{pnl_line}{reason_line}"
    )


def notify_fib_error(symbol: str, detail: str) -> None:
    text = (detail or "")[:420]
    send_warn(f"{symbol.upper()} FIB error\n{text}")
