"""Public Pumpstall Telegram channel (@pumpstall).

★ SHORT candidates (setup only), #OPEN when grid orders are placed,
CLOSE / DCA / etc. with PnL %% from avg entry (never volume/USDT size),
and a morning #REPORT summary (yesterday / week / month).

Env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_PUMPSTALL_CHAT_ID=@pumpstall   (falls back to TELEGRAM_CHAT_ID)
  PUMPSTALL_SITE_URL=https://pumpstall.idoneo.dev
  PUMPSTALL_TZ=Europe/Madrid
  PUMPSTALL_SUMMARY_HOUR=8
  PUMPSTALL_BANK_PCT=30          # after report: % of yesterday net futures PnL → spot (0=off)
  PUMPSTALL_BANK_MIN_USDT=1
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


def _load_dotenv() -> None:
    """Load ROOT/.env into os.environ without overwriting existing vars."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)


_load_dotenv()


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    return (
        os.getenv("TELEGRAM_PUMPSTALL_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )


def is_configured() -> bool:
    return bool(_token() and chat_id())


def config_status() -> str:
    tok = bool(_token())
    chat = chat_id()
    return (
        f"token={'yes' if tok else 'NO'} "
        f"chat={chat or 'NO'} "
        f"configured={is_configured()}"
    )


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
    """Adaptive decimals so micro-priced walls stay distinguishable (e.g. 0.000623 vs 0.000631)."""
    import math

    p = abs(float(price))
    if p == 0:
        return "0"
    # Keep ~4 significant digits after the first non-zero decimal digit.
    order = int(math.floor(math.log10(p)))
    decimals = max(4, min(12, -order + 4))
    s = f"{float(price):.{decimals}f}".rstrip("0").rstrip(".")
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


def pnl_pct_from_entry(
    entry: float | None,
    mark: float | None,
    direction: str | None = None,
) -> float | None:
    """Finandy-style %%: price distance from avg entry. Independent of leverage/size.

    LONG  → (mark − entry) / entry × 100
    SHORT → (entry − mark) / entry × 100

    After DCA compensation the avg entry moves, so the %% shrinks naturally.
    """
    e = float(entry or 0)
    m = float(mark or 0)
    if e <= 0 or m <= 0:
        return None
    side = (direction or "SHORT").strip().upper()
    if side == "LONG":
        return (m - e) / e * 100.0
    return (e - m) / e * 100.0


def pnl_pct_from_close(
    pnl_usdt: float | None,
    notional: float | None,
    leverage: float | int | None = None,
    *,
    entry: float | None = None,
    mark: float | None = None,
    direction: str | None = None,
) -> float | None:
    """Public PnL %% from avg entry (Finandy). Falls back to unlevered pnl/notional.

    ``leverage`` is accepted for call-site compatibility but never used.
    Never exposes size.
    """
    pct = pnl_pct_from_entry(entry, mark, direction)
    if pct is not None:
        return pct
    if pnl_usdt is None:
        return None
    notion = abs(float(notional or 0))
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

    # ★ ideal only — not a filled trade. Real #OPEN comes when orders are placed.
    return (
        f"⭐ <b>IDEAL</b> · <b>{sym}</b>\n"
        f"\n"
        f"{chg_emoji} <b>{change_24h:+.1f}%</b> 24h · "
        f"🚀 pump {_html_escape(f'{pump_7d_pct:.0f}%')} · "
        f"🎯 score <b>{score:.1f}</b>\n"
        f"\n"
        f"{_html_escape(metrics)}\n"
        f"\n"
        f"{walls_line}\n"
        f"\n"
        f"<i>{note_line}</i>"
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
    reason_line = ""
    if reason and str(reason).strip():
        reason_line = f"\n{_html_escape(str(reason).strip())}"
    return (
        f"🥳 <b>#CLOSE {side}</b> · <b>{sym}</b>\n"
        f"PnL · <b>{pnl_pct:+.2f}%</b>"
        f"{reason_line}"
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
    entry: float | None = None,
    mark: float | None = None,
    pnl_pct: float | None = None,
    reason: str | None = None,
) -> bool:
    """Public close: %% from avg entry only — never volume / USDT size."""
    if not is_configured():
        return False
    pct = pnl_pct
    if pct is None:
        pct = pnl_pct_from_close(
            pnl_usdt,
            notional,
            leverage,
            entry=entry,
            mark=mark,
            direction=direction,
        )
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

    title = yesterday.strftime("%d %b %Y")

    return (
        f"📊 <b>#REPORT</b> · {_html_escape(title)}\n"
        f"\n"
        f"{line('Day', day_sum, day_n)}\n"
        f"{line('Week', week_sum, week_n)}\n"
        f"{line('Month', month_sum, month_n)}"
    )


def maybe_send_daily_summary(*, force: bool = False) -> bool:
    """Send once per local day at PUMPSTALL_SUMMARY_HOUR (default 08:00).

    On success the watch also runs ``botctl.sweep`` (orphan orders on flat symbols).
    After today's report exists, banks ``PUMPSTALL_BANK_PCT`` of yesterday's net
    futures PnL to spot (see ``pumpstall_bank``).
    """
    _load_dotenv()
    if not is_configured():
        logger.warning("Pumpstall summary skipped — %s", config_status())
        return False
    tz = _tz()
    now = datetime.now(tz)
    hour = int(os.getenv("PUMPSTALL_SUMMARY_HOUR", "8") or 8)
    if not force and now.hour != hour:
        return False

    today_s = now.date().isoformat()
    already = False
    if SUMMARY_STAMP.is_file():
        already = SUMMARY_STAMP.read_text(encoding="utf-8").strip() == today_s

    newly_sent = False
    if not already or force:
        text = format_daily_summary(as_of=now.date())
        ok = _send_html(text)
        if ok:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SUMMARY_STAMP.write_text(today_s + "\n", encoding="utf-8")
            newly_sent = True
            already = True
            logger.info("Pumpstall daily summary sent for %s", today_s)
        else:
            logger.warning("Pumpstall summary send failed — %s", config_status())
            if not already:
                return False

    if already:
        try:
            from pumpstall_bank import maybe_bank_profits_to_spot

            # Never force-transfer on summary --force (avoids double bank while testing).
            maybe_bank_profits_to_spot(as_of=now.date(), force=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pumpstall bank step failed: %s", exc)

    return newly_sent
