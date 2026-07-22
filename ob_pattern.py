"""Higher-timeframe pattern filter + candlestick detectors for OB scalp.

HTF vote (tag ``htf``): continuity / volume / ATR / Bollinger / FVG.
Candlestick votes: classic patterns tagged by name (hammer, engulfing, …).

Default interval: 5m (HTF + candles) + 15m (FVG backdrop).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from futures_scan import FAPI_BASE, fetch_klines
from ob_candles import detect_candlesticks


@dataclass
class PatternSnapshot:
    interval: str
    body_pct: float
    body_ratio: float
    direction: str  # bull | bear | doji
    continuity: int
    vol_ratio: float
    atr_pct: float
    bb_pos: float  # 0 lower → 1 upper
    fvg_bias: str  # long | short | none
    allow_long: bool
    allow_short: bool
    reason: str
    candles: list[str] = field(default_factory=list)
    ts: float = 0.0

    def log_line(self) -> str:
        cndl = "+".join(self.candles) if self.candles else "none"
        return (
            f"{self.interval} body={self.body_pct:.3f}% ratio={self.body_ratio:.2f} "
            f"dir={self.direction} cont={self.continuity} volx={self.vol_ratio:.2f} "
            f"atr={self.atr_pct:.3f}% bb={self.bb_pos:.2f} fvg={self.fvg_bias} "
            f"htf L={self.allow_long} S={self.allow_short} candles={cndl} · {self.reason}"
        )


@dataclass
class PatternConfig:
    interval: str = "5m"
    fvg_interval: str = "15m"
    min_body_ratio: float = 0.40
    min_body_pct: float = 0.05
    min_continuity: int = 1
    min_vol_ratio: float = 0.90
    min_atr_pct: float = 0.04
    bb_period: int = 20
    bb_std: float = 2.0
    # Soft BB extremes: block longs near upper, shorts near lower unless FVG agrees
    bb_long_max: float = 0.92
    bb_short_min: float = 0.08
    cache_sec: float = 45.0


_CACHE: dict[str, PatternSnapshot] = {}


def _ohlcv(klines: list[list]) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    v = [float(k[5]) for k in klines]
    return o, h, l, c, v


def _atr_pct(high: list[float], low: list[float], close: list[float], period: int = 14) -> float:
    if len(close) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(close)):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        trs.append(tr)
    window = trs[-period:]
    atr = sum(window) / len(window)
    last = close[-1]
    return (atr / last * 100.0) if last > 0 else 0.0


def _bollinger_pos(closes: list[float], period: int = 20, num_std: float = 2.0) -> float:
    if len(closes) < period:
        return 0.5
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    if std <= 0:
        return 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = upper - lower
    if width <= 0:
        return 0.5
    pos = (closes[-1] - lower) / width
    return max(0.0, min(1.0, pos))


def _candle_metrics(o: float, h: float, l: float, c: float) -> tuple[str, float, float]:
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    body_ratio = body / rng
    body_pct = (body / c * 100.0) if c > 0 else 0.0
    if c > o and body_ratio >= 0.35:
        direction = "bull"
    elif c < o and body_ratio >= 0.35:
        direction = "bear"
    else:
        direction = "doji"
    return direction, body_pct, body_ratio


def _continuity(opens: list[float], closes: list[float]) -> tuple[str, int]:
    """Count consecutive closed candles in the same direction."""
    if len(closes) < 2:
        return "doji", 0
    dirs = []
    for o, c in zip(opens, closes):
        if c > o:
            dirs.append("bull")
        elif c < o:
            dirs.append("bear")
        else:
            dirs.append("doji")
    last = dirs[-1]
    if last == "doji":
        return last, 0
    n = 0
    for d in reversed(dirs):
        if d == last:
            n += 1
        else:
            break
    return last, n


def _find_fvg_bias(high: list[float], low: list[float], close: list[float]) -> str:
    """Detect nearest unfilled 3-candle FVG relative to last close."""
    if len(close) < 5:
        return "none"
    px = close[-1]
    bull_dist = None
    bear_dist = None
    for i in range(len(close) - 3, max(1, len(close) - 20), -1):
        gap_lo = high[i]
        gap_hi = low[i + 2]
        if gap_hi > gap_lo and px >= gap_lo:
            d = abs(px - (gap_lo + gap_hi) / 2)
            if bull_dist is None or d < bull_dist:
                bull_dist = d
        gap_hi_b = low[i]
        gap_lo_b = high[i + 2]
        if gap_hi_b > gap_lo_b and px <= gap_hi_b:
            d = abs(px - (gap_lo_b + gap_hi_b) / 2)
            if bear_dist is None or d < bear_dist:
                bear_dist = d
    if bull_dist is None and bear_dist is None:
        return "none"
    if bear_dist is None:
        return "long"
    if bull_dist is None:
        return "short"
    return "long" if bull_dist <= bear_dist else "short"


def evaluate_pattern(
    symbol: str,
    *,
    cfg: PatternConfig | None = None,
    base: str = FAPI_BASE,
    force: bool = False,
) -> PatternSnapshot | None:
    cfg = cfg or PatternConfig()
    key = f"{symbol.upper()}:{cfg.interval}:{cfg.fvg_interval}"
    cached = _CACHE.get(key)
    now = time.time()
    if not force and cached and (now - cached.ts) < cfg.cache_sec:
        return cached

    need = max(cfg.bb_period + 5, 40)
    k5 = fetch_klines(base, symbol.upper(), cfg.interval, need)
    if len(k5) < cfg.bb_period + 3:
        return None

    # Drop the last (still-forming) candle for continuity decisions.
    closed = k5[:-1]
    o, h, l, c, v = _ohlcv(closed)
    direction, body_pct, body_ratio = _candle_metrics(o[-1], h[-1], l[-1], c[-1])
    cont_dir, continuity = _continuity(o, c)
    if cont_dir != "doji":
        direction = cont_dir

    avg_vol = sum(v[-21:-1]) / max(1, len(v[-21:-1])) if len(v) > 2 else (v[-1] or 1.0)
    vol_ratio = (v[-1] / avg_vol) if avg_vol > 0 else 1.0
    atr_pct = _atr_pct(h, l, c)
    bb_pos = _bollinger_pos(c, period=cfg.bb_period, num_std=cfg.bb_std)

    fvg_bias = "none"
    try:
        k15 = fetch_klines(base, symbol.upper(), cfg.fvg_interval, 40)
        if len(k15) >= 8:
            _, h15, l15, c15, _ = _ohlcv(k15[:-1])
            fvg_bias = _find_fvg_bias(h15, l15, c15)
    except Exception:
        fvg_bias = "none"

    reasons: list[str] = []
    strong_body = body_ratio >= cfg.min_body_ratio and body_pct >= cfg.min_body_pct
    strong_vol = vol_ratio >= cfg.min_vol_ratio
    enough_atr = atr_pct >= cfg.min_atr_pct
    cont_ok = continuity >= cfg.min_continuity

    allow_long = False
    allow_short = False

    if not enough_atr:
        reasons.append(f"atr {atr_pct:.3f}%<{cfg.min_atr_pct:g}%")
    else:
        # Core: directional body + (continuity OR volume OR FVG bias)
        long_core = (
            direction == "bull"
            and strong_body
            and (cont_ok or strong_vol or fvg_bias == "long")
        )
        long_bb_ok = bb_pos <= cfg.bb_long_max or fvg_bias == "long"
        if long_core and long_bb_ok:
            allow_long = True
            if fvg_bias == "long" and not (cont_ok or strong_vol):
                reasons.append("fvg long boost")
        else:
            miss = []
            if direction != "bull":
                miss.append("not bull candle")
            if not strong_body:
                miss.append(f"weak body {body_ratio:.2f}")
            if not cont_ok and not strong_vol and fvg_bias != "long":
                miss.append(f"cont {continuity}<{cfg.min_continuity} / volx {vol_ratio:.2f}")
            if not long_bb_ok:
                miss.append(f"bb {bb_pos:.2f} overbought")
            reasons.append("long: " + ", ".join(miss) if miss else "long denied")

        short_core = (
            direction == "bear"
            and strong_body
            and (cont_ok or strong_vol or fvg_bias == "short")
        )
        short_bb_ok = bb_pos >= cfg.bb_short_min or fvg_bias == "short"
        if short_core and short_bb_ok:
            allow_short = True
            if fvg_bias == "short" and not (cont_ok or strong_vol):
                reasons.append("fvg short boost")
        else:
            miss = []
            if direction != "bear":
                miss.append("not bear candle")
            if not strong_body:
                miss.append(f"weak body {body_ratio:.2f}")
            if not cont_ok and not strong_vol and fvg_bias != "short":
                miss.append(f"cont {continuity}<{cfg.min_continuity} / volx {vol_ratio:.2f}")
            if not short_bb_ok:
                miss.append(f"bb {bb_pos:.2f} oversold")
            reasons.append("short: " + ", ".join(miss) if miss else "short denied")

    if allow_long or allow_short:
        reasons = [r for r in reasons if "boost" in r] or ["htf ok"]

    candles = detect_candlesticks(o, h, l, c)
    if candles:
        reasons.append("candles=" + "+".join(candles))

    snap = PatternSnapshot(
        interval=cfg.interval,
        body_pct=body_pct,
        body_ratio=body_ratio,
        direction=direction,
        continuity=continuity,
        vol_ratio=vol_ratio,
        atr_pct=atr_pct,
        bb_pos=bb_pos,
        fvg_bias=fvg_bias,
        allow_long=allow_long,
        allow_short=allow_short,
        reason="; ".join(reasons) if reasons else "ok",
        candles=candles,
        ts=now,
    )
    _CACHE[key] = snap
    return snap


def pattern_allows(signal: str, snap: PatternSnapshot | None) -> bool:
    if snap is None:
        return True
    if signal == "long":
        return snap.allow_long
    if signal == "short":
        return snap.allow_short
    return True


def format_pattern_console(snap: PatternSnapshot) -> str:
    from orderbook_dca_grid import CYAN, DIM, GREEN, RED, RESET, YELLOW

    al = f"{GREEN}L{RESET}" if snap.allow_long else f"{DIM}·{RESET}"
    ash = f"{RED}S{RESET}" if snap.allow_short else f"{DIM}·{RESET}"
    if snap.direction == "bull":
        dcol = GREEN
    elif snap.direction == "bear":
        dcol = RED
    else:
        dcol = YELLOW
    cndl = "+".join(snap.candles) if snap.candles else "—"
    return (
        f"  {DIM}PAT {snap.interval}{RESET}  "
        f"{dcol}{snap.direction}{RESET} body {snap.body_ratio:.2f} "
        f"cont {snap.continuity} volx {CYAN}{snap.vol_ratio:.2f}{RESET} "
        f"atr {snap.atr_pct:.2f}% bb {snap.bb_pos:.2f} fvg {snap.fvg_bias} "
        f"htf {al}/{ash}  candles {CYAN}{cndl}{RESET}"
    )
