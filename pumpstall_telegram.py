"""Public Pumpstall Telegram channel (@pumpstall).

★ SHORT candidates (setup only), #OPEN when grid orders are placed,
CLOSE / DCA / etc. with PnL %% from avg entry (never volume/USDT size),
and a morning #REPORT: futures wallet ROI from Binance income
(REALIZED_PNL + COMMISSION) ÷ equity — not the sum of per-trade price %%.

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
STATS_FILE = STATE_DIR / "pumpstall_stats.json"
SUMMARY_STAMP = STATE_DIR / "pumpstall_summary_sent_date.txt"
EQUITY_LOG = STATE_DIR / "pumpstall_equity.jsonl"
STATS_TRADE_LIMIT = 200


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


def _ath_sl_ideal_line(symbol: str, *, from_price: float | None = None) -> str | None:
    """🛡️ line for IDEAL cards: planned full SL at historical ATH + pct.

    When ``from_price`` is set (usually first ask wall), append the total %%
    climb from that level to the SL — same style as wall gaps.
    """
    try:
        from exits.risk_reduce import (
            ath_sl_pct,
            ath_sl_trigger,
            enabled,
            fetch_historical_ath,
        )
    except Exception:
        return None
    if not enabled():
        return None
    try:
        ath = fetch_historical_ath(symbol)
    except Exception:
        return None
    if not ath or ath <= 0:
        return None
    try:
        pct = ath_sl_pct()
        sl = ath_sl_trigger(ath)
    except Exception:
        return None
    ath_s = _html_escape(_fmt_wall_px(ath))
    sl_s = _html_escape(_fmt_wall_px(sl))
    total = ""
    try:
        base = float(from_price or 0)
        if base > 0 and sl > 0:
            total_pct = (sl - base) / base * 100.0
            total = f"  (+{total_pct:.2f}%)"
    except (TypeError, ValueError):
        total = ""
    return f"🛡️ <b>SL</b> · ATH {ath_s} +{pct:g}% → <b>{sl_s}</b>{total}"


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
    ath_sl_line: str | None = None,
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

    from_px = walls[0] if walls else None
    shield = (ath_sl_line or "").strip() or _ath_sl_ideal_line(symbol, from_price=from_px) or ""
    shield_block = f"{shield}\n\n" if shield else ""

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
        f"{shield_block}"
        f"<i>{note_line}</i>"
    )


def _close_emoji(pnl_pct: float) -> str:
    """🥳 win · 😢 loss · 🤖 flat."""
    if pnl_pct < 0:
        return "😢"
    if pnl_pct > 0:
        return "🥳"
    return "🤖"


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
    emoji = _close_emoji(float(pnl_pct))
    return (
        f"{emoji} <b>#CLOSE {side}</b> · <b>{sym}</b>\n"
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
    exit_mode: str | None = None,
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
        record_trade(
            symbol, direction, pct, reason=reason, exit_mode=exit_mode,
        )
        logger.info("Pumpstall CLOSE sent %s pnl=%+.2f%%", symbol.upper(), pct)
    return ok


def _mode_from_reason(reason: str | None) -> str | None:
    """Who actually flattened — overlays beat the primary --exit on /stats."""
    r = (reason or "").strip().lower()
    if not r:
        return None
    if "pullback" in r:
        return "pullback"
    if "ratchet" in r:
        return "ratchet"
    if "ob-flip" in r or "ob long" in r or "ob short" in r:
        return "ob"
    if "eql" in r or "eqh" in r or "structure" in r:
        return "structure"
    if "partial" in r or r.startswith("tp1"):
        return "partial"
    if "trail" in r or "runner" in r:
        return "trailing"
    if r.startswith("be") or "break-even" in r or "breakeven" in r:
        return "be"
    return None


def infer_exit_mode(reason: str | None, exit_mode: str | None = None) -> str:
    """Credit the closer from reason when present (ratchet + structure overlay)."""
    from_reason = _mode_from_reason(reason)
    if from_reason:
        return from_reason
    raw = (exit_mode or "").strip().lower()
    if raw and raw not in ("none", "unknown", "?"):
        if raw in ("eql", "eq", "eqh", "structure_tp"):
            return "structure"
        if raw in ("trail",):
            return "trailing"
        if raw in ("pb", "pull", "giveback"):
            return "pullback"
        if raw in ("support-be", "support_be", "ratchet-be", "ratchet_be", "levels"):
            return "ratchet"
        if raw in ("ob-long", "ob_long", "be-ob", "be_ob"):
            return "ob"
        return raw
    return "unknown"


def record_trade(
    symbol: str,
    direction: str,
    pnl_pct: float,
    *,
    reason: str | None = None,
    exit_mode: str | None = None,
    when: datetime | None = None,
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = when or datetime.now(timezone.utc)
    mode = infer_exit_mode(reason, exit_mode)
    row = {
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "symbol": symbol.upper(),
        "direction": (direction or "SHORT").upper(),
        "pnl_pct": float(pnl_pct),
        "reason": (reason or "").strip() or None,
        "exit_mode": mode,
    }
    with TRADES_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        push_stats_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pumpstall stats snapshot skipped: %s", exc)


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


def _append_equity_snapshot(wallet_usdt: float, *, as_of: date) -> None:
    """Record futures wallet once per local day (for period start equity)."""
    day_s = as_of.isoformat()
    for row in _load_equity_snapshots():
        if str(row.get("date", "")) == day_s:
            return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "date": day_s,
        "wallet_usdt": float(wallet_usdt),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with EQUITY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_equity_snapshots() -> list[dict[str, Any]]:
    if not EQUITY_LOG.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in EQUITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _equity_on_or_before(snapshots: list[dict[str, Any]], day: date) -> float | None:
    """Latest snapshot wallet on or before ``day``."""
    best: tuple[date, float] | None = None
    for row in snapshots:
        try:
            d = date.fromisoformat(str(row.get("date", "")))
            w = float(row.get("wallet_usdt", 0) or 0)
        except (TypeError, ValueError):
            continue
        if d > day or w <= 0:
            continue
        if best is None or d > best[0]:
            best = (d, w)
    return None if best is None else best[1]


def _wallet_roi_pct(net_usdt: float, start_equity: float) -> float | None:
    if start_equity <= 0:
        return None
    return net_usdt / start_equity * 100.0


def _normalize_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    reason = row.get("reason")
    mode = infer_exit_mode(
        str(reason) if reason else None,
        str(row.get("exit_mode") or "") or None,
    )
    try:
        pnl = float(row.get("pnl_pct", 0) or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    return {
        "ts": row.get("ts"),
        "symbol": str(row.get("symbol") or "").upper(),
        "direction": str(row.get("direction") or "SHORT").upper(),
        "pnl_pct": pnl,
        "reason": (str(reason).strip() if reason else None) or None,
        "exit_mode": mode,
    }


def _exit_mode_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Win-rate and avg PnL+ by exit mode (for private stats UI)."""
    buckets: dict[str, dict[str, Any]] = {}
    for raw in rows:
        t = _normalize_trade_row(raw)
        mode = t["exit_mode"]
        b = buckets.setdefault(
            mode,
            {"exit_mode": mode, "n": 0, "wins": 0, "sum_pnl": 0.0, "sum_win": 0.0},
        )
        pnl = float(t["pnl_pct"])
        b["n"] += 1
        b["sum_pnl"] += pnl
        if pnl > 0:
            b["wins"] += 1
            b["sum_win"] += pnl
    out: list[dict[str, Any]] = []
    for mode, b in buckets.items():
        n = int(b["n"])
        wins = int(b["wins"])
        out.append({
            "exit_mode": mode,
            "n": n,
            "wins": wins,
            "win_rate_pct": (wins / n * 100.0) if n else 0.0,
            "avg_pnl_pct": (float(b["sum_pnl"]) / n) if n else 0.0,
            "avg_win_pct": (float(b["sum_win"]) / wins) if wins else 0.0,
        })
    out.sort(key=lambda x: (-x["win_rate_pct"], -x["avg_win_pct"], -x["n"]))
    return out


def build_report_payload(*, as_of: date | None = None) -> dict[str, Any]:
    """Structured Day/Week/Month wallet ROI (same windows as #REPORT)."""
    _load_dotenv()
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
    day_n = len(_trades_between(rows, day_start, day_end))
    week_n = len(_trades_between(rows, week_start, day_end))
    month_n = len(_trades_between(rows, month_start, day_end))

    day_net = week_net = month_net = 0.0
    wallet_now = 0.0
    snaps = _load_equity_snapshots()
    api_ok = False
    try:
        from pumpstall_bank import (
            fetch_period_net_pnl_usdt,
            futures_wallet_usdt,
            _keys,
        )

        api, sec = _keys()
        if api and sec:
            day_net, _ = fetch_period_net_pnl_usdt(api, sec, day_start, day_end)
            week_net, _ = fetch_period_net_pnl_usdt(api, sec, week_start, day_end)
            month_net, _ = fetch_period_net_pnl_usdt(api, sec, month_start, day_end)
            wallet_now = futures_wallet_usdt(api, sec, "USDT")
            api_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pumpstall #REPORT wallet ROI fetch failed: %s", exc)

    def start_eq(period_start_date: date, net: float) -> float:
        snap = _equity_on_or_before(snaps, period_start_date)
        if snap is not None and snap > 0:
            return snap
        if wallet_now > 0:
            return max(wallet_now - net, wallet_now * 0.25, 1.0)
        return 0.0

    day_pct = _wallet_roi_pct(day_net, start_eq(yesterday, day_net))
    week_pct = _wallet_roi_pct(week_net, start_eq(week_start_date, week_net))
    month_pct = _wallet_roi_pct(month_net, start_eq(month_start.date(), month_net))

    def period(pct: float | None, net: float, n: int) -> dict[str, Any]:
        return {"pct": pct, "closes": n, "net_usdt": round(net, 4) if api_ok else None}

    return {
        "as_of": yesterday.isoformat(),
        "as_of_label": yesterday.strftime("%d %b %Y"),
        "api_ok": api_ok,
        "day": period(day_pct, day_net, day_n),
        "week": period(week_pct, week_net, week_n),
        "month": period(month_pct, month_net, month_n),
    }


def _merge_report_closes(
    previous: dict[str, Any] | None,
    lightweight: dict[str, Any],
) -> dict[str, Any]:
    """Keep wallet ROI from the last full report; refresh close counts only.

    Lightweight pushes run on every #CLOSE and must not wipe ``pct`` / ``api_ok``
    from the morning ``include_wallet_report=True`` snapshot.
    """
    if not isinstance(previous, dict) or not previous.get("api_ok"):
        return lightweight
    out = dict(lightweight)
    out["api_ok"] = True
    # Prefer previous as_of labels when they match a successful wallet fetch
    if previous.get("as_of"):
        out["as_of"] = previous.get("as_of")
    if previous.get("as_of_label"):
        out["as_of_label"] = previous.get("as_of_label")
    for key in ("day", "week", "month"):
        prev_b = previous.get(key) if isinstance(previous.get(key), dict) else {}
        new_b = lightweight.get(key) if isinstance(lightweight.get(key), dict) else {}
        merged = dict(new_b)
        if prev_b.get("pct") is not None:
            merged["pct"] = prev_b.get("pct")
        if prev_b.get("net_usdt") is not None:
            merged["net_usdt"] = prev_b.get("net_usdt")
        out[key] = merged
    return out


def build_stats_payload(
    *,
    as_of: date | None = None,
    trade_limit: int = STATS_TRADE_LIMIT,
    include_wallet_report: bool = False,
    previous_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Private stats for the unlisted web page (no Binance keys on the site)."""
    rows = _load_trades()
    normalized = [_normalize_trade_row(r) for r in rows]
    # Newest first for the UI
    normalized.sort(key=lambda t: str(t.get("ts") or ""), reverse=True)
    limit = max(1, int(trade_limit))
    trades = normalized[:limit]
    payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "trades": trades,
        "trade_count": len(normalized),
        "by_exit": _exit_mode_stats(normalized),
    }
    if include_wallet_report:
        try:
            payload["report"] = build_report_payload(as_of=as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stats report block skipped: %s", exc)
            # Keep last good wallet ROI rather than blanking the page
            payload["report"] = previous_report if isinstance(previous_report, dict) else None
    else:
        # Lightweight: closes-only windows from the local log (no Binance call)
        try:
            tz = _tz()
            today = as_of or datetime.now(tz).date()
            yesterday = today - timedelta(days=1)
            day_start = datetime(
                yesterday.year, yesterday.month, yesterday.day, tzinfo=tz,
            )
            day_end = day_start + timedelta(days=1)
            week_start_date = yesterday - timedelta(days=yesterday.weekday())
            week_start = datetime(
                week_start_date.year, week_start_date.month, week_start_date.day,
                tzinfo=tz,
            )
            month_start = datetime(yesterday.year, yesterday.month, 1, tzinfo=tz)
            lightweight = {
                "as_of": yesterday.isoformat(),
                "as_of_label": yesterday.strftime("%d %b %Y"),
                "api_ok": False,
                "day": {"pct": None, "closes": len(_trades_between(rows, day_start, day_end))},
                "week": {"pct": None, "closes": len(_trades_between(rows, week_start, day_end))},
                "month": {
                    "pct": None,
                    "closes": len(_trades_between(rows, month_start, day_end)),
                },
            }
            payload["report"] = _merge_report_closes(previous_report, lightweight)
        except Exception:
            payload["report"] = previous_report if isinstance(previous_report, dict) else None
    return payload


def write_stats_snapshot(
    *,
    path: Path | None = None,
    include_wallet_report: bool = False,
) -> Path:
    """Write `.state/pumpstall_stats.json` for the Pumpstall private stats page."""
    _load_dotenv()
    out = path or Path(
        os.getenv("PUMPSTALL_STATS_PATH", "").strip() or str(STATS_FILE),
    )
    if not out.is_absolute():
        out = ROOT / out
    previous_report: dict[str, Any] | None = None
    if out.is_file():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and isinstance(prev.get("report"), dict):
                previous_report = prev["report"]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            previous_report = None
    payload = build_stats_payload(
        include_wallet_report=include_wallet_report,
        previous_report=previous_report,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def push_stats_snapshot(
    *,
    include_wallet_report: bool = False,
) -> bool:
    """Write local stats JSON and POST it to the Pumpstall site (HTTPS ingest).

    Needed when VPS :8787 is firewalled from the web host.
    Env:
      PUMPSTALL_STATS_PUSH_URL=https://pumpstall.com/api/pumpstall-stats
      PUMPSTALL_STATS_PUSH_TOKEN=…  (or API_TOKEN / same as site PUMPSTALL_SCAN_TOKEN)
    """
    _load_dotenv()
    path = write_stats_snapshot(include_wallet_report=include_wallet_report)
    url = (os.getenv("PUMPSTALL_STATS_PUSH_URL") or "").strip()
    if not url:
        return True  # local snapshot only
    token = (
        (os.getenv("PUMPSTALL_STATS_PUSH_TOKEN") or "").strip()
        or (os.getenv("API_TOKEN") or "").strip()
    )
    if not token:
        logger.warning("Pumpstall stats push skipped: no PUMPSTALL_STATS_PUSH_TOKEN/API_TOKEN")
        return False
    try:
        body = path.read_bytes()
    except OSError as exc:
        logger.warning("Pumpstall stats push read failed: %s", exc)
        return False
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pumpstall-bot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= int(getattr(resp, "status", 200) or 200) < 300
            if not ok:
                logger.warning("Pumpstall stats push HTTP %s", getattr(resp, "status", "?"))
            return ok
    except urllib.error.HTTPError as exc:
        logger.warning("Pumpstall stats push HTTP %s: %s", exc.code, exc.reason)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pumpstall stats push failed: %s", exc)
        return False


def format_daily_summary(
    *,
    as_of: date | None = None,
    private: bool = False,
) -> str:
    """Yesterday + week/month-to-yesterday as futures wallet ROI.

    % = Binance income (REALIZED_PNL + COMMISSION) ÷ starting futures equity
    for that window. Close count still comes from local trade log.

    Public (``private=False``): percentages + closes only.
    Private ops (``private=True``): also includes USDT net PnL.
    """
    data = build_report_payload(as_of=as_of)
    api_ok = bool(data.get("api_ok"))

    def line(label: str, block: dict[str, Any]) -> str:
        pct = block.get("pct")
        n = int(block.get("closes") or 0)
        net = float(block.get("net_usdt") or 0)
        closes = f"{n} close{'s' if n != 1 else ''}"
        if pct is None:
            if private and api_ok:
                return f"{label:<5} · <b>{net:+.2f} USDT</b>  <i>({closes})</i>"
            return f"{label:<5} · <i>n/a</i>  <i>({closes})</i>"
        if private:
            return (
                f"{label:<5} · <b>{float(pct):+.2f}%</b>  "
                f"<i>({net:+.2f} USDT · {closes})</i>"
            )
        return f"{label:<5} · <b>{float(pct):+.2f}%</b>  <i>({closes})</i>"

    title = str(data.get("as_of_label") or "")
    foot = (
        "\n\n<i>wallet ROI · futures net PnL ÷ equity</i>"
        if api_ok
        else "\n\n<i>wallet ROI unavailable — check API keys</i>"
    )
    return (
        f"📊 <b>#REPORT</b> · {_html_escape(title)}\n"
        f"\n"
        f"{line('Day', data['day'])}\n"
        f"{line('Week', data['week'])}\n"
        f"{line('Month', data['month'])}"
        f"{foot}"
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
        # Public channel: % + closes only (no USDT).
        text = format_daily_summary(as_of=now.date(), private=False)
        ok = _send_html(text)
        if ok:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            SUMMARY_STAMP.write_text(today_s + "\n", encoding="utf-8")
            newly_sent = True
            already = True
            try:
                from pumpstall_bank import futures_wallet_usdt, _keys

                api, sec = _keys()
                if api and sec:
                    _append_equity_snapshot(
                        futures_wallet_usdt(api, sec, "USDT"),
                        as_of=now.date(),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pumpstall equity snapshot skipped: %s", exc)
            logger.info("Pumpstall daily summary sent for %s", today_s)
            try:
                push_stats_snapshot(include_wallet_report=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Pumpstall stats after report skipped: %s", exc)
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
