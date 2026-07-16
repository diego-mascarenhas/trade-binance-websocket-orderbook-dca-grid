"""1m EMA trend filter for OB scalp (slope + price vs slow EMA)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from futures_scan import FAPI_BASE, fetch_klines

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


@dataclass
class EmaSnapshot:
    close: float
    ema_fast: float
    ema_slow: float
    slope_pct: float
    trend: str
    allow_long: bool
    allow_short: bool
    fast_period: int = 7
    slow_period: int = 25

    def log_line(self) -> str:
        allow = "LONG" if self.allow_long else ("SHORT" if self.allow_short else "NONE")
        return (
            f"close={self.close:.4f} ema{self.fast_period}={self.ema_fast:.4f} "
            f"ema{self.slow_period}={self.ema_slow:.4f} slope={self.slope_pct:+.3f}% "
            f"trend={self.trend} allow={allow}"
        )


def ema_log_path(symbol: str) -> Path:
    path = LOG_ROOT / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path / "scalp_ema.log"


def append_ema_log(symbol: str, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    with open(ema_log_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(line)


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    k = 2 / (period + 1)
    out: list[float] = []
    ema = values[0]
    for v in values:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def fetch_ema_snapshot(
    symbol: str,
    *,
    interval: str = "1m",
    fast: int = 7,
    slow: int = 25,
    slope_bars: int = 5,
    slope_min_pct: float = 0.05,
    base: str = FAPI_BASE,
) -> EmaSnapshot | None:
    need = max(slow + slope_bars + 5, 40)
    klines = fetch_klines(base, symbol.upper(), interval, need)
    if len(klines) < slow + 2:
        return None
    closes = [float(k[4]) for k in klines]
    fast_s = _ema_series(closes, fast)
    slow_s = _ema_series(closes, slow)
    if len(fast_s) < slope_bars + 1:
        return None

    close = closes[-1]
    ema_fast = fast_s[-1]
    ema_slow = slow_s[-1]
    prev_fast = fast_s[-1 - slope_bars]
    slope_pct = ((ema_fast - prev_fast) / prev_fast * 100) if prev_fast > 0 else 0.0

    if slope_pct >= slope_min_pct and close > ema_slow:
        trend = "bullish"
    elif slope_pct <= -slope_min_pct and close < ema_slow:
        trend = "bearish"
    else:
        trend = "flat"

    snap = EmaSnapshot(
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        slope_pct=slope_pct,
        trend=trend,
        allow_long=trend == "bullish",
        allow_short=trend == "bearish",
        fast_period=fast,
        slow_period=slow,
    )
    return snap


def ema_allows(signal: str, snap: EmaSnapshot | None) -> bool:
    if snap is None:
        return True
    if signal == "long":
        return snap.allow_long
    if signal == "short":
        return snap.allow_short
    return True


def format_ema_console(snap: EmaSnapshot) -> str:
    from orderbook_dca_grid import CYAN, DIM, GREEN, RED, RESET

    f, s = snap.fast_period, snap.slow_period
    if snap.trend == "bullish":
        trend_c = GREEN
    elif snap.trend == "bearish":
        trend_c = RED
    else:
        trend_c = DIM
    al = f"{GREEN}L{RESET}" if snap.allow_long else f"{DIM}·{RESET}"
    ash = f"{RED}S{RESET}" if snap.allow_short else f"{DIM}·{RESET}"
    return (
        f"  {DIM}EMA{f}/{s} 1m{RESET}  "
        f"{CYAN}{snap.ema_fast:.2f}{RESET}/{snap.ema_slow:.2f}  "
        f"slope {snap.slope_pct:+.3f}%  "
        f"{trend_c}{snap.trend}{RESET}  allow {al}/{ash}"
    )
