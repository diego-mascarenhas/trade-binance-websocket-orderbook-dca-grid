"""Aggregate order-book depth snapshots into synthetic OHLC-style bars."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OBBar:
    t_open: float
    t_close: float
    mid_o: float
    mid_h: float
    mid_l: float
    mid_c: float
    spread_avg: float
    imbalance: float
    bid_vol: float
    ask_vol: float
    bid_wall_price: float
    bid_wall_qty: float
    ask_wall_price: float
    ask_wall_qty: float
    samples: int = 0

    def mid_change_pct(self) -> float:
        if self.mid_o <= 0:
            return 0.0
        return (self.mid_c - self.mid_o) / self.mid_o * 100


def _book_metrics(
    bids: list[list[float]],
    asks: list[list[float]],
    *,
    band_pct: float,
) -> dict[str, float]:
    if not bids or not asks:
        return {
            "mid": 0.0,
            "spread": 0.0,
            "imbalance": 0.5,
            "bid_vol": 0.0,
            "ask_vol": 0.0,
            "bid_wall_price": 0.0,
            "bid_wall_qty": 0.0,
            "ask_wall_price": 0.0,
            "ask_wall_qty": 0.0,
        }

    mid = (bids[0][0] + asks[0][0]) / 2
    spread = asks[0][0] - bids[0][0]
    lo = mid * (1 - band_pct / 100)
    hi = mid * (1 + band_pct / 100)

    bid_band = [(p, q) for p, q in bids if p >= lo and p <= mid]
    ask_band = [(p, q) for p, q in asks if p >= mid and p <= hi]
    bid_vol = sum(q for _, q in bid_band)
    ask_vol = sum(q for _, q in ask_band)
    total = bid_vol + ask_vol
    imb = (bid_vol / total) if total else 0.5

    bid_wall = max(bid_band, key=lambda x: x[1], default=(0.0, 0.0))
    ask_wall = max(ask_band, key=lambda x: x[1], default=(0.0, 0.0))

    return {
        "mid": mid,
        "spread": spread,
        "imbalance": imb,
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "bid_wall_price": bid_wall[0],
        "bid_wall_qty": bid_wall[1],
        "ask_wall_price": ask_wall[0],
        "ask_wall_qty": ask_wall[1],
    }


@dataclass
class BarBuilder:
    """Accumulate depth samples until bar_sec elapses, then emit one OBBar."""

    bar_sec: float
    band_pct: float = 1.0
    _t_open: float = 0.0
    _mids: list[float] = field(default_factory=list)
    _spreads: list[float] = field(default_factory=list)
    _last: dict[str, float] = field(default_factory=dict)
    _samples: int = 0

    def start_bar(self, now: float) -> None:
        self._t_open = now
        self._mids.clear()
        self._spreads.clear()
        self._last = {}
        self._samples = 0

    def add_sample(self, bids: list[list[float]], asks: list[list[float]], now: float) -> OBBar | None:
        m = _book_metrics(bids, asks, band_pct=self.band_pct)
        if m["mid"] <= 0:
            return None

        if self._samples == 0:
            self._t_open = now

        self._mids.append(m["mid"])
        self._spreads.append(m["spread"])
        self._last = m
        self._samples += 1

        if now - self._t_open < self.bar_sec:
            return None

        return OBBar(
            t_open=self._t_open,
            t_close=now,
            mid_o=self._mids[0],
            mid_h=max(self._mids),
            mid_l=min(self._mids),
            mid_c=self._mids[-1],
            spread_avg=sum(self._spreads) / len(self._spreads),
            imbalance=m["imbalance"],
            bid_vol=m["bid_vol"],
            ask_vol=m["ask_vol"],
            bid_wall_price=m["bid_wall_price"],
            bid_wall_qty=m["bid_wall_qty"],
            ask_wall_price=m["ask_wall_price"],
            ask_wall_qty=m["ask_wall_qty"],
            samples=self._samples,
        )

    def reset_after_bar(self, now: float) -> None:
        self.start_bar(now)


def depth_to_levels(depth: dict[str, Any]) -> tuple[list[list[float]], list[list[float]]]:
    bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]
    return bids, asks
