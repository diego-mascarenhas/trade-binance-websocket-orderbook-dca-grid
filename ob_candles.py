"""Classic candlestick pattern detectors for OB scalp multi-trigger tags."""

from __future__ import annotations

# name -> side
BULLISH_CANDLES = frozenset({
    "hammer",
    "inverted_hammer",
    "bullish_engulfing",
    "piercing",
    "morning_star",
    "three_white_soldiers",
    "bullish_harami",
    "dragonfly_doji",
    "bullish_marubozu",
})
BEARISH_CANDLES = frozenset({
    "hanging_man",
    "shooting_star",
    "bearish_engulfing",
    "dark_cloud_cover",
    "evening_star",
    "three_black_crows",
    "bearish_harami",
    "gravestone_doji",
    "bearish_marubozu",
})
ALL_CANDLE_NAMES = BULLISH_CANDLES | BEARISH_CANDLES


def candle_side(name: str) -> str:
    if name in BULLISH_CANDLES:
        return "long"
    if name in BEARISH_CANDLES:
        return "short"
    return "none"


def _parts(o: float, h: float, l: float, c: float) -> dict[str, float]:
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "o": o, "h": h, "l": l, "c": c,
        "rng": rng,
        "body": body,
        "body_ratio": body / rng,
        "upper": upper,
        "lower": lower,
        "upper_ratio": upper / rng,
        "lower_ratio": lower / rng,
        "bull": c > o,
        "bear": c < o,
    }


def detect_candlesticks(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[str]:
    """Return all classic patterns that fire on the latest closed candle(s)."""
    if len(closes) < 3:
        return []
    i = len(closes) - 1
    c0 = _parts(opens[i], highs[i], lows[i], closes[i])
    c1 = _parts(opens[i - 1], highs[i - 1], lows[i - 1], closes[i - 1])
    c2 = _parts(opens[i - 2], highs[i - 2], lows[i - 2], closes[i - 2]) if i >= 2 else None

    hits: list[str] = []

    # --- single-candle ---
    # Hammer: small body near top, long lower wick (bullish reversal context)
    if (
        c0["lower_ratio"] >= 0.55
        and c0["upper_ratio"] <= 0.15
        and c0["body_ratio"] <= 0.35
        and c0["bull"]
    ):
        hits.append("hammer")
    # Hanging man: same shape after up-move → bearish (shape only; we tag as hanging_man when bear close or prior bull)
    if (
        c0["lower_ratio"] >= 0.55
        and c0["upper_ratio"] <= 0.15
        and c0["body_ratio"] <= 0.35
        and c0["bear"]
    ):
        hits.append("hanging_man")

    # Inverted hammer / shooting star
    if (
        c0["upper_ratio"] >= 0.55
        and c0["lower_ratio"] <= 0.15
        and c0["body_ratio"] <= 0.35
        and c0["bull"]
    ):
        hits.append("inverted_hammer")
    if (
        c0["upper_ratio"] >= 0.55
        and c0["lower_ratio"] <= 0.15
        and c0["body_ratio"] <= 0.35
        and c0["bear"]
    ):
        hits.append("shooting_star")

    # Dragonfly / gravestone doji
    if c0["body_ratio"] <= 0.08:
        if c0["lower_ratio"] >= 0.60 and c0["upper_ratio"] <= 0.12:
            hits.append("dragonfly_doji")
        if c0["upper_ratio"] >= 0.60 and c0["lower_ratio"] <= 0.12:
            hits.append("gravestone_doji")

    # Marubozu: almost no wicks, strong body
    if c0["body_ratio"] >= 0.85:
        if c0["bull"]:
            hits.append("bullish_marubozu")
        elif c0["bear"]:
            hits.append("bearish_marubozu")

    # --- two-candle ---
    # Engulfing
    if c1["bear"] and c0["bull"] and c0["o"] <= c1["c"] and c0["c"] >= c1["o"] and c0["body"] > c1["body"]:
        hits.append("bullish_engulfing")
    if c1["bull"] and c0["bear"] and c0["o"] >= c1["c"] and c0["c"] <= c1["o"] and c0["body"] > c1["body"]:
        hits.append("bearish_engulfing")

    # Harami: small body inside prior body
    if c1["body"] > 0 and c0["body"] < c1["body"] * 0.6:
        inside = min(c0["o"], c0["c"]) >= min(c1["o"], c1["c"]) and max(c0["o"], c0["c"]) <= max(c1["o"], c1["c"])
        if inside and c1["bear"] and c0["bull"]:
            hits.append("bullish_harami")
        if inside and c1["bull"] and c0["bear"]:
            hits.append("bearish_harami")

    # Piercing / dark cloud
    mid1 = (c1["o"] + c1["c"]) / 2
    if c1["bear"] and c0["bull"] and c0["o"] < c1["c"] and c0["c"] > mid1 and c0["c"] < c1["o"]:
        hits.append("piercing")
    if c1["bull"] and c0["bear"] and c0["o"] > c1["c"] and c0["c"] < mid1 and c0["c"] > c1["o"]:
        hits.append("dark_cloud_cover")

    # --- three-candle ---
    if c2 is not None:
        # Morning star: bear → small → bull closing into first body
        if (
            c2["bear"]
            and c2["body_ratio"] >= 0.4
            and c1["body_ratio"] <= 0.30
            and c0["bull"]
            and c0["c"] > mid_of(c2)
        ):
            hits.append("morning_star")
        # Evening star
        if (
            c2["bull"]
            and c2["body_ratio"] >= 0.4
            and c1["body_ratio"] <= 0.30
            and c0["bear"]
            and c0["c"] < mid_of(c2)
        ):
            hits.append("evening_star")

        # Three white soldiers / three black crows
        if (
            c2["bull"] and c1["bull"] and c0["bull"]
            and c1["c"] > c2["c"] and c0["c"] > c1["c"]
            and c2["body_ratio"] >= 0.45 and c1["body_ratio"] >= 0.45 and c0["body_ratio"] >= 0.45
        ):
            hits.append("three_white_soldiers")
        if (
            c2["bear"] and c1["bear"] and c0["bear"]
            and c1["c"] < c2["c"] and c0["c"] < c1["c"]
            and c2["body_ratio"] >= 0.45 and c1["body_ratio"] >= 0.45 and c0["body_ratio"] >= 0.45
        ):
            hits.append("three_black_crows")

    # Stable unique order
    return sorted(set(hits), key=lambda n: (0 if n in BULLISH_CANDLES else 1, n))


def mid_of(parts: dict[str, float]) -> float:
    return (parts["o"] + parts["c"]) / 2
