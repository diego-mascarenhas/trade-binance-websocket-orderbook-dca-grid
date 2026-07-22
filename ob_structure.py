"""Market-structure triggers for OB scalp: iCHoCH, EQH, EQL.

Uses Binance futures klines (default 5m) to find swing highs/lows, then:
  - iCHoCH (Change of Character): break of recent structure against prior trend
  - EQH: equal swing highs (liquidity) → short bias when price is near
  - EQL: equal swing lows (liquidity) → long bias when price is near

Designed as multi-trigger sources (OR with other tags).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from futures_scan import FAPI_BASE, fetch_klines


@dataclass
class StructureSnapshot:
    interval: str
    choch: str  # long | short | none
    eqh: bool
    eql: bool
    allow_long: bool
    allow_short: bool
    eqh_level: float
    eql_level: float
    reason: str
    ts: float = 0.0

    def log_line(self) -> str:
        return (
            f"{self.interval} choch={self.choch} eqh={self.eqh}@{self.eqh_level:g} "
            f"eql={self.eql}@{self.eql_level:g} "
            f"allow L={self.allow_long} S={self.allow_short} · {self.reason}"
        )


@dataclass
class StructureConfig:
    interval: str = "5m"
    lookback: int = 48
    swing_left: int = 2
    swing_right: int = 2
    equal_tol_pct: float = 0.12  # EQH/EQL match tolerance
    near_pct: float = 0.35  # price must be within this of EQ level to fire
    choch_lookback_swings: int = 6
    cache_sec: float = 30.0


_CACHE: dict[str, StructureSnapshot] = {}


def _ohlc(klines: list[list]) -> tuple[list[float], list[float], list[float]]:
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    return h, l, c


def _swing_highs(high: list[float], left: int, right: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(left, len(high) - right):
        window = high[i - left : i + right + 1]
        if high[i] >= max(window) and high[i] > high[i - 1] and high[i] > high[i + 1]:
            out.append((i, high[i]))
    return out


def _swing_lows(low: list[float], left: int, right: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(left, len(low) - right):
        window = low[i - left : i + right + 1]
        if low[i] <= min(window) and low[i] < low[i - 1] and low[i] < low[i + 1]:
            out.append((i, low[i]))
    return out


def _equal_level(
    swings: list[tuple[int, float]],
    *,
    tol_pct: float,
) -> tuple[bool, float]:
    """Return (found, level) for the most recent equal pair."""
    if len(swings) < 2:
        return False, 0.0
    # Prefer the latest swing matched to any prior within tolerance
    for i in range(len(swings) - 1, 0, -1):
        _, a = swings[i]
        for j in range(i - 1, -1, -1):
            _, b = swings[j]
            mid = (a + b) / 2.0
            if mid <= 0:
                continue
            if abs(a - b) / mid * 100 <= tol_pct:
                return True, mid
    return False, 0.0


def _detect_choch(
    high: list[float],
    low: list[float],
    close: list[float],
    sh: list[tuple[int, float]],
    sl: list[tuple[int, float]],
    *,
    lookback_swings: int,
) -> tuple[str, str]:
    """Return (choch_side, reason).

    Bullish iCHoCH: after lower-high / lower-low sequence, close breaks last swing high.
    Bearish iCHoCH: after higher-high / higher-low sequence, close breaks last swing low.
    """
    if len(close) < 5 or (len(sh) < 2 and len(sl) < 2):
        return "none", "need more swings"

    sh_r = sh[-lookback_swings:]
    sl_r = sl[-lookback_swings:]
    last = close[-1]

    # Bearish structure (LH + LL) then break above last SH → bullish CHOCH
    if len(sh_r) >= 2 and len(sl_r) >= 2:
        lh = sh_r[-1][1] < sh_r[-2][1]
        ll = sl_r[-1][1] < sl_r[-2][1]
        last_sh = sh_r[-1][1]
        if lh and ll and last > last_sh:
            return "long", f"bullish iCHoCH break SH {last_sh:g}"

        hh = sh_r[-1][1] > sh_r[-2][1]
        hl = sl_r[-1][1] > sl_r[-2][1]
        last_sl = sl_r[-1][1]
        if hh and hl and last < last_sl:
            return "short", f"bearish iCHoCH break SL {last_sl:g}"

    # Softer: single break of prior swing against last two swings direction
    if len(sh_r) >= 2:
        last_sh = sh_r[-1][1]
        if sh_r[-1][1] < sh_r[-2][1] and last > last_sh:
            return "long", f"iCHoCH break LH {last_sh:g}"
    if len(sl_r) >= 2:
        last_sl = sl_r[-1][1]
        if sl_r[-1][1] > sl_r[-2][1] and last < last_sl:
            return "short", f"iCHoCH break HL {last_sl:g}"

    return "none", "no iCHoCH"


def fetch_structure(symbol: str, cfg: StructureConfig | None = None) -> StructureSnapshot:
    cfg = cfg or StructureConfig()
    key = f"{symbol.upper()}:{cfg.interval}:{cfg.lookback}"
    cached = _CACHE.get(key)
    now = time.time()
    if cached and now - cached.ts < cfg.cache_sec:
        return cached

    klines = fetch_klines(FAPI_BASE, symbol.upper(), cfg.interval, cfg.lookback)
    if len(klines) < cfg.swing_left + cfg.swing_right + 6:
        snap = StructureSnapshot(
            interval=cfg.interval,
            choch="none",
            eqh=False,
            eql=False,
            allow_long=False,
            allow_short=False,
            eqh_level=0.0,
            eql_level=0.0,
            reason="not enough bars",
            ts=now,
        )
        _CACHE[key] = snap
        return snap

    # Drop still-forming candle
    high, low, close = _ohlc(klines[:-1])
    if len(close) < cfg.swing_left + cfg.swing_right + 5:
        snap = StructureSnapshot(
            interval=cfg.interval,
            choch="none",
            eqh=False,
            eql=False,
            allow_long=False,
            allow_short=False,
            eqh_level=0.0,
            eql_level=0.0,
            reason="not enough closed bars",
            ts=now,
        )
        _CACHE[key] = snap
        return snap

    sh = _swing_highs(high, cfg.swing_left, cfg.swing_right)
    sl = _swing_lows(low, cfg.swing_left, cfg.swing_right)
    choch, choch_reason = _detect_choch(
        high, low, close, sh, sl, lookback_swings=cfg.choch_lookback_swings,
    )
    eqh, eqh_lvl = _equal_level(sh, tol_pct=cfg.equal_tol_pct)
    eql, eql_lvl = _equal_level(sl, tol_pct=cfg.equal_tol_pct)

    px = close[-1]
    near_eqh = False
    near_eql = False
    if eqh and eqh_lvl > 0:
        near_eqh = abs(px - eqh_lvl) / eqh_lvl * 100 <= cfg.near_pct
    if eql and eql_lvl > 0:
        near_eql = abs(px - eql_lvl) / eql_lvl * 100 <= cfg.near_pct

    # Trigger bias:
    #   iCHoCH long/short → that side
    #   EQH near → short (liquidity above / rejection zone)
    #   EQL near → long
    allow_long = choch == "long" or near_eql
    allow_short = choch == "short" or near_eqh

    parts = [choch_reason]
    if near_eqh:
        parts.append(f"EQH@{eqh_lvl:g}")
    elif eqh:
        parts.append(f"EQH far@{eqh_lvl:g}")
    if near_eql:
        parts.append(f"EQL@{eql_lvl:g}")
    elif eql:
        parts.append(f"EQL far@{eql_lvl:g}")

    snap = StructureSnapshot(
        interval=cfg.interval,
        choch=choch,
        eqh=near_eqh,
        eql=near_eql,
        allow_long=allow_long,
        allow_short=allow_short,
        eqh_level=eqh_lvl if eqh else 0.0,
        eql_level=eql_lvl if eql else 0.0,
        reason="; ".join(parts),
        ts=now,
    )
    _CACHE[key] = snap
    return snap


def format_structure_console(snap: StructureSnapshot) -> str:
    from orderbook_dca_grid import CYAN, DIM, RESET

    return (
        f"{DIM}structure {snap.interval}{RESET}  "
        f"{CYAN}choch={snap.choch}{RESET}  "
        f"eqh={'Y' if snap.eqh else 'n'} eql={'Y' if snap.eql else 'n'}  "
        f"{DIM}{snap.reason}{RESET}"
    )
