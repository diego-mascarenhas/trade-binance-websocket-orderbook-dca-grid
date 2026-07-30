"""Backfill bar dataset from scalp stdout log (for faster ML bootstrap)."""

from __future__ import annotations

import re
from pathlib import Path

from ob_scalp_dataset import BarRecord, append_bar, bars_path, load_bars


_BAR_RE = re.compile(
    r"bar (\d{2}:\d{2}:\d{2}).*mid ([\d.]+) \(([+-][\d.]+)%\).*imb ([\d.]+)%"
)


def backfill_from_stdout(symbol: str) -> int:
    stdout = Path(".run/logs") / symbol.upper() / "scalp_stdout.log"
    if not stdout.exists():
        return 0
    existing_ts = {r.ts for r in load_bars(symbol)}
    added = 0
    today = __import__("datetime").date.today().isoformat()
    for line in stdout.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _BAR_RE.search(line)
        if not m:
            continue
        ts = f"{today} {m.group(1)}"
        if ts in existing_ts:
            continue
        mid = float(m.group(2))
        chg = float(m.group(3))
        imb = float(m.group(4)) / 100
        mid_o = mid / (1 + chg / 100) if chg else mid
        rec = BarRecord(
            ts=ts,
            t_close=0.0,
            mid_o=mid_o,
            mid_h=max(mid_o, mid),
            mid_l=min(mid_o, mid),
            mid_c=mid,
            mid_chg_pct=chg,
            imbalance=imb,
            spread_avg=0.0,
            bid_vol=0.0,
            ask_vol=0.0,
            bid_wall_qty=0.0,
            ask_wall_qty=0.0,
        )
        append_bar(symbol, rec)
        existing_ts.add(ts)
        added += 1
    return added
