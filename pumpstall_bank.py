"""After the daily #REPORT, move a cut of yesterday's net futures PnL to spot.

Uses Binance USDⓈ-M income (REALIZED_PNL + COMMISSION) for the report day
(yesterday in PUMPSTALL_TZ), then universal transfer UMFUTURE → MAIN.

Env:
  PUMPSTALL_BANK_PCT=30          # 0 disables
  PUMPSTALL_BANK_MIN_USDT=1      # skip tiny transfers
  PUMPSTALL_BANK_ASSET=USDT
  PUMPSTALL_TZ=Europe/Madrid
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
BANK_STAMP = STATE_DIR / "pumpstall_bank_sent_date.txt"

FAPI_BASE = os.getenv("FAPI_BASE", "https://fapi.binance.com").rstrip("/")
SAPI_BASE = os.getenv("SAPI_BASE", "https://api.binance.com").rstrip("/")


def _load_dotenv() -> None:
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


def _tz() -> ZoneInfo:
    name = os.getenv("PUMPSTALL_TZ", "Europe/Madrid").strip() or "Europe/Madrid"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def bank_pct() -> float:
    try:
        return float(os.getenv("PUMPSTALL_BANK_PCT", "30") or 0)
    except (TypeError, ValueError):
        return 0.0


def bank_min_usdt() -> float:
    try:
        return float(os.getenv("PUMPSTALL_BANK_MIN_USDT", "1") or 1)
    except (TypeError, ValueError):
        return 1.0


def bank_asset() -> str:
    return (os.getenv("PUMPSTALL_BANK_ASSET", "USDT") or "USDT").upper()


def _keys() -> tuple[str, str]:
    from orderbook_dca_grid import load_keys

    return load_keys(None)


def _signed(
    base: str,
    method: str,
    path: str,
    params: dict[str, Any],
    api: str,
    sec: str,
    *,
    recv_window: int = 15000,
) -> Any:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = recv_window
    query = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{query}&signature={sig}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": api})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None


def _yesterday_window(as_of: date) -> tuple[datetime, datetime, date]:
    """Report day = calendar yesterday relative to as_of (local TZ)."""
    tz = _tz()
    yesterday = as_of - timedelta(days=1)
    start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end, yesterday


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _fetch_income_chunk(
    api: str,
    sec: str,
    income_type: str,
    start_ms: int,
    end_ms: int,
) -> float:
    """Sum one incomeType over [start_ms, end_ms). Window must be ≤ 7 days (Binance)."""
    total = 0.0
    cursor_end = end_ms
    guard = 0
    while guard < 40:
        guard += 1
        rows = _signed(
            FAPI_BASE,
            "GET",
            "/fapi/v1/income",
            {
                "incomeType": income_type,
                "startTime": start_ms,
                "endTime": cursor_end,
                "limit": 1000,
            },
            api,
            sec,
        )
        if not isinstance(rows, list) or not rows:
            break
        oldest_ts = None
        for row in rows:
            try:
                ts = int(row.get("time", 0))
                if ts < start_ms or ts >= end_ms:
                    continue
                total += float(row.get("income", 0) or 0)
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
            except (TypeError, ValueError):
                continue
        if len(rows) < 1000 or oldest_ts is None:
            break
        cursor_end = oldest_ts - 1
        if cursor_end < start_ms:
            break
    return total


def fetch_period_net_pnl_usdt(
    api: str,
    sec: str,
    start: datetime,
    end: datetime,
) -> tuple[float, dict[str, float]]:
    """Net USDT for [start, end): REALIZED_PNL + COMMISSION (funding excluded).

    Binance caps each /income request at 7 days — longer ranges are chunked.
    """
    buckets: dict[str, float] = {"REALIZED_PNL": 0.0, "COMMISSION": 0.0}
    if end <= start:
        return 0.0, buckets

    # Walk forward in ≤7d slices (Binance income limit).
    chunk = timedelta(days=7)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + chunk, end)
        start_ms, end_ms = _ms(cursor), _ms(chunk_end)
        for income_type in ("REALIZED_PNL", "COMMISSION"):
            buckets[income_type] += _fetch_income_chunk(
                api, sec, income_type, start_ms, end_ms,
            )
        cursor = chunk_end

    net = buckets["REALIZED_PNL"] + buckets["COMMISSION"]
    return net, buckets


def fetch_day_net_pnl_usdt(
    api: str,
    sec: str,
    *,
    as_of: date,
) -> tuple[float, date, dict[str, float]]:
    """Net USDT for report day: REALIZED_PNL + COMMISSION (funding excluded)."""
    start, end, day = _yesterday_window(as_of)
    net, buckets = fetch_period_net_pnl_usdt(api, sec, start, end)
    return net, day, buckets


def futures_wallet_usdt(api: str, sec: str, asset: str = "USDT") -> float:
    """Total futures wallet balance for asset (not just available)."""
    rows = _signed(FAPI_BASE, "GET", "/fapi/v2/balance", {}, api, sec)
    if not isinstance(rows, list):
        return 0.0
    for row in rows:
        if str(row.get("asset", "")).upper() == asset.upper():
            try:
                return float(row.get("balance", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def futures_available_usdt(api: str, sec: str, asset: str) -> float:
    rows = _signed(FAPI_BASE, "GET", "/fapi/v2/balance", {}, api, sec)
    if not isinstance(rows, list):
        return 0.0
    for row in rows:
        if str(row.get("asset", "")).upper() == asset:
            try:
                return float(row.get("availableBalance", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def transfer_umfutures_to_spot(
    api: str,
    sec: str,
    *,
    asset: str,
    amount: float,
) -> dict[str, Any]:
    amt = str(
        Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    )
    return _signed(
        SAPI_BASE,
        "POST",
        "/sapi/v1/asset/transfer",
        {
            "type": "UMFUTURE_MAIN",
            "asset": asset,
            "amount": amt,
        },
        api,
        sec,
    )


def _round_down_2(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _notify_ops(text: str) -> None:
    try:
        import telegram_notify

        telegram_notify._send_sync(text[:4096])  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bank ops notify failed: %s", exc)


def maybe_bank_profits_to_spot(
    *,
    as_of: date | None = None,
    force: bool = False,
) -> str | None:
    """Transfer bank_pct() of yesterday's net futures PnL to spot. Once per day.

    Returns a short status string when an attempt was made, else None.
    """
    _load_dotenv()
    pct = bank_pct()
    if pct <= 0:
        return None

    tz = _tz()
    today = as_of or datetime.now(tz).date()
    today_s = today.isoformat()
    if not force and BANK_STAMP.is_file():
        if BANK_STAMP.read_text(encoding="utf-8").strip() == today_s:
            return None

    api, sec = _keys()
    if not api or not sec:
        msg = "🏦 Bank skipped — missing BINANCE_API_KEY / BINANCE_SECRET_KEY"
        logger.warning(msg)
        _notify_ops(msg)
        return msg

    try:
        net, day, buckets = fetch_day_net_pnl_usdt(api, sec, as_of=today)
    except Exception as exc:  # noqa: BLE001
        msg = f"🏦 Bank failed reading income: {exc}"
        logger.warning(msg)
        _notify_ops(msg)
        return msg

    if net <= 0:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        BANK_STAMP.write_text(today_s + "\n", encoding="utf-8")
        msg = (
            f"🏦 Bank: no cut for {day.isoformat()} "
            f"(net {net:+.2f}, realized {buckets['REALIZED_PNL']:+.2f}, "
            f"fees {buckets['COMMISSION']:+.2f})"
        )
        logger.info(msg)
        _notify_ops(msg)
        return msg

    cut = _round_down_2(net * (pct / 100.0))
    min_amt = bank_min_usdt()
    asset = bank_asset()
    if cut < min_amt:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        BANK_STAMP.write_text(today_s + "\n", encoding="utf-8")
        msg = (
            f"🏦 Bank: cut {cut:.2f} below min {min_amt:.2f} "
            f"(net {net:+.2f} · {pct:g}%) — skipped"
        )
        logger.info(msg)
        _notify_ops(msg)
        return msg

    try:
        avail = futures_available_usdt(api, sec, asset)
    except Exception as exc:  # noqa: BLE001
        msg = f"🏦 Bank failed reading futures balance: {exc}"
        logger.warning(msg)
        _notify_ops(msg)
        return msg

    send_amt = _round_down_2(min(cut, max(0.0, avail)))
    if send_amt < min_amt:
        msg = (
            f"🏦 Bank: insufficient futures free balance "
            f"(need {cut:.2f}, free {avail:.2f})"
        )
        logger.warning(msg)
        _notify_ops(msg)
        return msg

    try:
        resp = transfer_umfutures_to_spot(api, sec, asset=asset, amount=send_amt)
    except Exception as exc:  # noqa: BLE001
        msg = f"🏦 Bank transfer failed: {exc}"
        logger.warning(msg)
        _notify_ops(msg)
        return msg

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BANK_STAMP.write_text(today_s + "\n", encoding="utf-8")
    tran_id = resp.get("tranId", resp) if isinstance(resp, dict) else resp
    msg = (
        f"🏦 Banked {send_amt:.2f} {asset} → spot "
        f"({pct:g}% of {net:+.2f} net · {day.isoformat()})\n"
        f"tranId={tran_id}"
    )
    logger.info(msg)
    _notify_ops(msg)
    return msg
