"""Public Pumpstall Telegram channel (@pumpstall).

★ OPEN SHORT setups, CLOSE with PnL % only (never volume/USDT size),
and a morning summary (yesterday / week / month).

Env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_PUMPSTALL_CHAT_ID=@pumpstall   (falls back to TELEGRAM_CHAT_ID)
  PUMPSTALL_SITE_URL=https://pumpstall.idoneo.dev
  PUMPSTALL_TZ=Europe/Madrid
  PUMPSTALL_SUMMARY_HOUR=8
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
TRADES_FILE = STATE_DIR / "pumpstall_trades.jsonl"
SUMMARY_STAMP = STATE_DIR / "pumpstall_summary_sent_date.txt"


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    return (
        os.getenv("TELEGRAM_PUMPSTALL_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )


def is_configured() -> bool:
    return bool(_token() and chat_id())


def _site_url() -> str:
    return os.getenv(
        "PUMPSTALL_SITE_URL", "https://pumpstall.idoneo.dev"
    ).strip().rstrip("/")


def _tz() -> ZoneInfo:
    name = os.getenv("PUMPSTALL_TZ", "Europe/Madrid").strip() or "Europe/Madrid"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_wall_px(price: float) -> str:
    s = f"{price:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _send_html(text: str) -> bool:
    token = _token()
    chat = chat_id()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Pumpstall Telegram send failed: %s", exc)
        return False


def pnl_pct_from_close(
    pnl_usdt: float | None,
    notional: float | None,
    leverage: float | int | None,
) -> float | None:
    """ROI %% on margin when possible; else %% of notional. Never exposes size."""
    if pnl_usdt is None:
        return None
    notion = abs(float(notional or 0))
    lev = float(leverage or 0)
    if lev > 0 and notion > 0:
        margin = notion / lev
        if margin > 0:
            return pnl_usdt / margin * 100.0
    if notion > 0:
        return pnl_usdt / notion * 100.0
    return None


def format_open_signal(
    *,
    symbol: str,
    change_24h: float,
    pump_7d_pct: float,
    score: float,
    near_high_pct: float,
    near_regime_pct: float,
    sharp_pct: float,
    stall_score: float,
    ask_walls: int,
    ask_span_pct: float,
    wall_prices: list[float],
    note: str = "",
) -> str:
    sym = _html_escape(symbol.upper())
    site = _site_url()
    chg_emoji = "📈" if change_24h >= 0 else "📉"
    metrics = ", ".join(
        [
            f"Near · {near_high_pct:.0f}%",
            f"Regime · {near_regime_pct:.0f}%",
            f"Sharp · {sharp_pct:.0f}",
            f"Stall · {stall_score:.0f}",
            f"Walls · {ask_walls}",
            f"Span · {ask_span_pct:.1f}%",
        ]
    )
    walls = [float(p) for p in wall_prices[:6] if p is not None]
    if walls:
        parts = [f"<b>Price ~ {_html_escape(_fmt_wall_px(walls[0]))}</b>"]
        for a, b in zip(walls, walls[1:]):
            pct = (b - a) / a * 100 if a else 0.0
            parts.append(f"→ {_html_escape(_fmt_wall_px(b))} (+{pct:.2f}%)")
        walls_line = "🧱 <b>Ask walls</b> · " + " ".join(parts)
    else:
        walls_line = "🧱 <b>Ask walls</b> · <i>no ask walls</i>"

    note_line = _html_escape(note.strip()) if note and note.strip() else (
        f"1D regime {near_regime_pct:.0f}% · sharp {sharp_pct:.0f} · "
        f"stall {stall_score:.0f} · {ask_walls} walls / {ask_span_pct:.1f}%"
    )
    site_label = site.replace("https://", "").replace("http://", "")

    return (
        f"⭐ <b>OPEN SHORT</b> · <b>{sym}</b>\n"
        f"\n"
        f"{chg_emoji} <b>{change_24h:+.1f}%</b> 24h · "
        f"🚀 pump {_html_escape(f'{pump_7d_pct:.0f}%')} · "
        f"🎯 score <b>{score:.1f}</b>\n"
        f"\n"
        f"{_html_escape(metrics)}\n"
        f"\n"
        f"{walls_line}\n"
        f"\n"
        f"<i>{note_line}</i>\n"
        f"\n"
        f'<a href="{_html_escape(site)}">{_html_escape(site_label)}</a>'
        f" · software, not advice"
    )


def format_close_signal(
    *,
    symbol: str,
    direction: str,
    pnl_pct: float,
    reason: str | None = None,
) -> str:
    sym = _html_escape(symbol.upper())
    side = _html_escape((direction or "SHORT").upper())
    emoji = "✅" if pnl_pct >= 0 else "❌"
    reason_line = ""
    if reason and str(reason).strip():
        reason_line = f"\n{_html_escape(str(reason).strip())}"
    site = _site_url()
    site_label = site.replace("https://", "").replace("http://", "")
    return (
        f"{emoji} <b>CLOSE {side}</b> · <b>{sym}</b>\n"
        f"PnL · <b>{pnl_pct:+.2f}%</b>"
        f"{reason_line}\n"
        f"\n"
        f'<a href="{_html_escape(site)}">{_html_escape(site_label)}</a>'
        f" · software, not advice"
    )


def notify_open_hit(hit: Any) -> bool:
    if isinstance(hit, dict):
        text = format_open_signal(
            symbol=str(hit.get("symbol", "")),
            change_24h=float(hit.get("change_24h", 0)),
            pump_7d_pct=float(hit.get("pump_7d_pct", 0)),
            score=float(hit.get("score", 0)),
            near_high_pct=float(hit.get("near_high_pct", 0)),
            near_regime_pct=float(hit.get("near_regime_pct", 0)),
            sharp_pct=float(hit.get("sharp_pct", 0)),
            stall_score=float(hit.get("stall_score", 0)),
            ask_walls=int(hit.get("ask_walls", 0)),
            ask_span_pct=float(hit.get("ask_span_pct", 0)),
            wall_prices=list(hit.get("wall_prices") or []),
            note=str(hit.get("note") or ""),
        )
    else:
        text = format_open_signal(
            symbol=str(hit.symbol),
            change_24h=float(hit.change_24h),
            pump_7d_pct=float(hit.pump_7d_pct),
            score=float(hit.score),
            near_high_pct=float(hit.near_high_pct),
            near_regime_pct=float(hit.near_regime_pct),
            sharp_pct=float(hit.sharp_pct),
            stall_score=float(hit.stall_score),
            ask_walls=int(hit.ask_walls),
            ask_span_pct=float(hit.ask_span_pct),
            wall_prices=list(hit.wall_prices or []),
            note=str(getattr(hit, "note", "") or ""),
        )
    ok = _send_html(text)
    if ok:
        logger.info("Pumpstall OPEN sent %s", getattr(hit, "symbol", hit))
    return ok


def notify_close(
    symbol: str,
    direction: str,
    *,
    pnl_usdt: float | None = None,
    notional: float | None = None,
    leverage: float | int | None = None,
    pnl_pct: float | None = None,
    reason: str | None = None,
) -> bool:
    """Public close: percentage only — never volume / USDT size."""
    if not is_configured():
        return False
    pct = pnl_pct
    if pct is None:
        pct = pnl_pct_from_close(pnl_usdt, notional, leverage)
    if pct is None:
        logger.info("Pumpstall CLOSE skipped %s (no pnl %%)", symbol)
        return False
    text = format_close_signal(
        symbol=symbol, direction=direction, pnl_pct=pct, reason=reason,
    )
    ok = _send_html(text)
    if ok:
        record_trade(symbol, direction, pct, reason=reason)
        logger.info("Pumpstall CLOSE sent %s pnl=%+.2f%%", symbol.upper(), pct)
    return ok


def record_trade(
    symbol: str,
    direction: str,
    pnl_pct: float,
    *,
    reason: str | None = None,
    when: datetime | None = None,
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = when or datetime.now(timezone.utc)
    row = {
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "symbol": symbol.upper(),
        "direction": (direction or "SHORT").upper(),
        "pnl_pct": float(pnl_pct),
        "reason": (reason or "").strip() or None,
    }
    with TRADES_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_trades() -> list[dict[str, Any]]:
    if not TRADES_FILE.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _sum_pct(rows: list[dict[str, Any]]) -> tuple[float, int]:
    total = 0.0
    n = 0
    for r in rows:
        try:
            total += float(r.get("pnl_pct", 0))
            n += 1
        except (TypeError, ValueError):
            continue
    return total, n


def _trades_between(
    rows: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    for r in rows:
        ts = _parse_ts(str(r.get("ts", "")))
        if ts is None:
            continue
        if start <= ts < end:
            picked.append(r)
    return picked


def format_daily_summary(*, as_of: date | None = None) -> str:
    """Yesterday + calendar week-to-yesterday + month-to-yesterday (local TZ)."""
    tz = _tz()
    today = as_of or datetime.now(tz).date()
    yesterday = today - timedelta(days=1)

    day_start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    week_start_date = yesterday - timedelta(days=yesterday.weekday())  # Monday
    week_start = datetime(
        week_start_date.year, week_start_date.month, week_start_date.day, tzinfo=tz,
    )

    month_start = datetime(yesterday.year, yesterday.month, 1, tzinfo=tz)

    rows = _load_trades()
    day_rows = _trades_between(rows, day_start, day_end)
    week_rows = _trades_between(rows, week_start, day_end)
    month_rows = _trades_between(rows, month_start, day_end)

    day_sum, day_n = _sum_pct(day_rows)
    week_sum, week_n = _sum_pct(week_rows)
    month_sum, month_n = _sum_pct(month_rows)

    def line(label: str, total: float, n: int) -> str:
        return f"{label:<5} · <b>{total:+.2f}%</b>  <i>({n} trade{'s' if n != 1 else ''})</i>"

    site = _site_url()
    site_label = site.replace("https://", "").replace("http://", "")
    title = yesterday.strftime("%d %b %Y")

    return (
        f"📊 <b>Pumpstall report</b> · {_html_escape(title)}\n"
        f"\n"
        f"{line('Day', day_sum, day_n)}\n"
        f"{line('Week', week_sum, week_n)}\n"
        f"{line('Month', month_sum, month_n)}\n"
        f"\n"
        f"<i>Sum of closed trade ROI %% — not account equity. "
        f"Software, not advice.</i>\n"
        f"\n"
        f'<a href="{_html_escape(site)}">{_html_escape(site_label)}</a>'
    )


def maybe_send_daily_summary(*, force: bool = False) -> bool:
    """Send once per local day at PUMPSTALL_SUMMARY_HOUR (default 08:00)."""
    if not is_configured():
        return False
    tz = _tz()
    now = datetime.now(tz)
    hour = int(os.getenv("PUMPSTALL_SUMMARY_HOUR", "8") or 8)
    if not force and now.hour != hour:
        return False

    today_s = now.date().isoformat()
    if not force and SUMMARY_STAMP.is_file():
        if SUMMARY_STAMP.read_text(encoding="utf-8").strip() == today_s:
            return False

    text = format_daily_summary(as_of=now.date())
    ok = _send_html(text)
    if ok:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_STAMP.write_text(today_s + "\n", encoding="utf-8")
        logger.info("Pumpstall daily summary sent for %s", today_s)
    return ok
