"""RSI and Stochastic oscillator triggers for OB scalp multi-trigger OR."""

from __future__ import annotations

import time
from dataclasses import dataclass

from futures_scan import FAPI_BASE, fetch_klines

DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

_CACHE: dict[str, OscillatorSnapshot] = {}


@dataclass
class OscillatorConfig:
    interval: str = "5m"
    lookback: int = 80
    cache_sec: float = 25.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    stoch_k: int = 14
    stoch_d: int = 3
    stoch_oversold: float = 20.0
    stoch_overbought: float = 80.0
    adx_period: int = 14
    adx_min: float = 20.0  # trend strength threshold (UI filter)


@dataclass
class OscillatorSnapshot:
    interval: str
    rsi: float
    stoch_k: float
    stoch_d: float
    rsi_side: str  # long | short | none
    stoch_side: str  # long | short | none
    reason: str
    ts: float = 0.0
    adx: float = 0.0
    di_plus: float = 0.0
    di_minus: float = 0.0
    adx_side: str = "none"  # long | short | none (DI bias when ADX ≥ min)


def _rsi(closes: list[float], period: int) -> list[float]:
    if len(closes) < period + 2:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out: list[float] = [50.0] * period  # pad so indices align with closes[period:]
    if avg_l <= 1e-12:
        out.append(100.0)
    else:
        out.append(100.0 - (100.0 / (1.0 + avg_g / avg_l)))
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l <= 1e-12:
            out.append(100.0)
        else:
            out.append(100.0 - (100.0 / (1.0 + avg_g / avg_l)))
    return out


def _stoch(
    high: list[float],
    low: list[float],
    close: list[float],
    k_period: int,
    d_period: int,
) -> tuple[list[float], list[float]]:
    if len(close) < k_period + d_period:
        return [], []
    raw_k: list[float] = []
    for i in range(k_period - 1, len(close)):
        window_h = high[i - k_period + 1 : i + 1]
        window_l = low[i - k_period + 1 : i + 1]
        hh = max(window_h)
        ll = min(window_l)
        if hh - ll <= 1e-12:
            raw_k.append(50.0)
        else:
            raw_k.append((close[i] - ll) / (hh - ll) * 100.0)
    # %D = SMA of %K
    d_vals: list[float] = []
    for i in range(len(raw_k)):
        if i + 1 < d_period:
            d_vals.append(sum(raw_k[: i + 1]) / (i + 1))
        else:
            d_vals.append(sum(raw_k[i - d_period + 1 : i + 1]) / d_period)
    return raw_k, d_vals


def _rsi_side(rsi: float, prev: float, *, oversold: float, overbought: float) -> str:
    # Mean-reversion: zone or cross out of extreme
    if rsi <= oversold or (prev <= oversold and rsi > prev):
        return "long"
    if rsi >= overbought or (prev >= overbought and rsi < prev):
        return "short"
    return "none"


def _stoch_side(
    k: float,
    d: float,
    prev_k: float,
    prev_d: float,
    *,
    oversold: float,
    overbought: float,
) -> str:
    cross_up = prev_k <= prev_d and k > d
    cross_down = prev_k >= prev_d and k < d
    if (k <= oversold and (cross_up or k >= d)) or (cross_up and k <= oversold + 10):
        return "long"
    if (k >= overbought and (cross_down or k <= d)) or (cross_down and k >= overbought - 10):
        return "short"
    return "none"


def _adx(
    high: list[float],
    low: list[float],
    close: list[float],
    period: int,
) -> tuple[float, float, float]:
    """Wilder ADX. Returns (adx, +DI, -DI) for the latest closed bar."""
    n = len(close)
    if n < period + 2:
        return 0.0, 0.0, 0.0
    tr_l: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_l.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )
    if len(tr_l) < period:
        return 0.0, 0.0, 0.0
    atr = sum(tr_l[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    dx_vals: list[float] = []

    def _di(sm: float, atr_v: float) -> float:
        return 0.0 if atr_v <= 1e-12 else 100.0 * sm / atr_v

    di_p = _di(sm_plus, atr)
    di_m = _di(sm_minus, atr)
    denom = di_p + di_m
    dx_vals.append(0.0 if denom <= 1e-12 else 100.0 * abs(di_p - di_m) / denom)

    for i in range(period, len(tr_l)):
        atr = atr - (atr / period) + tr_l[i]
        sm_plus = sm_plus - (sm_plus / period) + plus_dm[i]
        sm_minus = sm_minus - (sm_minus / period) + minus_dm[i]
        di_p = _di(sm_plus, atr)
        di_m = _di(sm_minus, atr)
        denom = di_p + di_m
        dx_vals.append(0.0 if denom <= 1e-12 else 100.0 * abs(di_p - di_m) / denom)

    if len(dx_vals) < period:
        adx = sum(dx_vals) / len(dx_vals) if dx_vals else 0.0
        return adx, di_p, di_m
    adx = sum(dx_vals[:period]) / period
    for i in range(period, len(dx_vals)):
        adx = (adx * (period - 1) + dx_vals[i]) / period
    return adx, di_p, di_m


def fetch_oscillators(symbol: str, cfg: OscillatorConfig | None = None) -> OscillatorSnapshot:
    cfg = cfg or OscillatorConfig()
    key = (
        f"{symbol.upper()}:{cfg.interval}:{cfg.rsi_period}:{cfg.stoch_k}:"
        f"{cfg.rsi_oversold}:{cfg.stoch_oversold}:{cfg.adx_period}:{cfg.adx_min}"
    )
    cached = _CACHE.get(key)
    now = time.time()
    if cached and now - cached.ts < cfg.cache_sec:
        return cached

    need = max(
        cfg.lookback,
        cfg.rsi_period + 10,
        cfg.stoch_k + cfg.stoch_d + 10,
        cfg.adx_period * 3 + 5,
    )
    klines = fetch_klines(FAPI_BASE, symbol.upper(), cfg.interval, need)
    # Drop forming candle
    bars = klines[:-1] if len(klines) > 1 else klines
    if len(bars) < cfg.rsi_period + 5:
        snap = OscillatorSnapshot(
            interval=cfg.interval,
            rsi=50.0,
            stoch_k=50.0,
            stoch_d=50.0,
            rsi_side="none",
            stoch_side="none",
            reason="not enough bars",
            ts=now,
        )
        _CACHE[key] = snap
        return snap

    high = [float(k[2]) for k in bars]
    low = [float(k[3]) for k in bars]
    close = [float(k[4]) for k in bars]

    rsi_s = _rsi(close, cfg.rsi_period)
    k_s, d_s = _stoch(high, low, close, cfg.stoch_k, cfg.stoch_d)
    adx, di_p, di_m = _adx(high, low, close, cfg.adx_period)

    rsi = rsi_s[-1] if rsi_s else 50.0
    prev_rsi = rsi_s[-2] if len(rsi_s) >= 2 else rsi
    sk = k_s[-1] if k_s else 50.0
    sd = d_s[-1] if d_s else 50.0
    prev_k = k_s[-2] if len(k_s) >= 2 else sk
    prev_d = d_s[-2] if len(d_s) >= 2 else sd

    rsi_side = _rsi_side(
        rsi, prev_rsi, oversold=cfg.rsi_oversold, overbought=cfg.rsi_overbought,
    )
    stoch_side = _stoch_side(
        sk, sd, prev_k, prev_d,
        oversold=cfg.stoch_oversold, overbought=cfg.stoch_overbought,
    )
    adx_side = "none"
    if adx >= cfg.adx_min:
        if di_p > di_m:
            adx_side = "long"
        elif di_m > di_p:
            adx_side = "short"

    parts = [
        f"RSI={rsi:.1f}",
        f"Stoch={sk:.1f}/{sd:.1f}",
        f"ADX={adx:.1f}",
        f"+DI={di_p:.1f}",
        f"-DI={di_m:.1f}",
    ]
    if rsi_side != "none":
        parts.append(f"rsi→{rsi_side}")
    if stoch_side != "none":
        parts.append(f"stoch→{stoch_side}")
    if adx_side != "none":
        parts.append(f"adx→{adx_side}")

    snap = OscillatorSnapshot(
        interval=cfg.interval,
        rsi=rsi,
        stoch_k=sk,
        stoch_d=sd,
        rsi_side=rsi_side,
        stoch_side=stoch_side,
        reason=" · ".join(parts),
        ts=now,
        adx=adx,
        di_plus=di_p,
        di_minus=di_m,
        adx_side=adx_side,
    )
    _CACHE[key] = snap
    return snap


def format_oscillators_console(snap: OscillatorSnapshot) -> str:
    def side_c(side: str) -> str:
        if side == "long":
            return f"{GREEN}{side}{RESET}"
        if side == "short":
            return f"{RED}{side}{RESET}"
        return f"{DIM}none{RESET}"

    return (
        f"{DIM}osc {snap.interval}{RESET}  "
        f"{CYAN}RSI={snap.rsi:.1f}{RESET}→{side_c(snap.rsi_side)}  "
        f"{CYAN}Stoch={snap.stoch_k:.1f}/{snap.stoch_d:.1f}{RESET}→{side_c(snap.stoch_side)}  "
        f"{CYAN}ADX={snap.adx:.1f}{RESET}→{side_c(snap.adx_side)}"
    )
