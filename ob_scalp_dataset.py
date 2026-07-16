"""Persist synthetic OB bars for ML / autotune."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ob_bars import OBBar
from ob_ema import EmaSnapshot

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


@dataclass
class BarRecord:
    ts: str
    t_close: float
    mid_o: float
    mid_h: float
    mid_l: float
    mid_c: float
    mid_chg_pct: float
    imbalance: float
    spread_avg: float
    bid_vol: float
    ask_vol: float
    bid_wall_qty: float
    ask_wall_qty: float
    ema_slope_pct: float | None = None
    ema_trend: str | None = None
    ema_allow_long: bool | None = None
    ema_allow_short: bool | None = None
    ob_signal: str | None = None

    @classmethod
    def from_bar(
        cls,
        bar: OBBar,
        *,
        ob_signal: str | None = None,
        ema: EmaSnapshot | None = None,
    ) -> BarRecord:
        return cls(
            ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bar.t_close)),
            t_close=bar.t_close,
            mid_o=bar.mid_o,
            mid_h=bar.mid_h,
            mid_l=bar.mid_l,
            mid_c=bar.mid_c,
            mid_chg_pct=bar.mid_change_pct(),
            imbalance=bar.imbalance,
            spread_avg=bar.spread_avg,
            bid_vol=bar.bid_vol,
            ask_vol=bar.ask_vol,
            bid_wall_qty=bar.bid_wall_qty,
            ask_wall_qty=bar.ask_wall_qty,
            ob_signal=ob_signal,
            ema_slope_pct=ema.slope_pct if ema else None,
            ema_trend=ema.trend if ema else None,
            ema_allow_long=ema.allow_long if ema else None,
            ema_allow_short=ema.allow_short if ema else None,
        )


def bars_path(symbol: str) -> Path:
    path = LOG_ROOT / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path / "scalp_bars.jsonl"


def append_bar(symbol: str, record: BarRecord) -> None:
    line = json.dumps(asdict(record), separators=(",", ":"))
    with open(bars_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_bars(symbol: str, *, limit: int = 0) -> list[BarRecord]:
    path = bars_path(symbol)
    if not path.exists():
        return []
    rows: list[BarRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data: dict[str, Any] = json.loads(line)
            rows.append(BarRecord(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    if limit > 0 and len(rows) > limit:
        return rows[-limit:]
    return rows
