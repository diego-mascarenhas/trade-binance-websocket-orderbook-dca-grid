"""Read scalp session activity from logs (signals, trades, entries)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ob_scalp_adaptive import adaptive_path, load_adaptive

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


def _read_jsonl_tail(path: Path, *, max_lines: int = 400) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max_lines:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def last_signal_at(symbol: str) -> float:
    """Unix ts of last bar with raw OB signal long/short."""
    sym = symbol.upper()
    best = 0.0
    for rec in _read_jsonl_tail(LOG_ROOT / sym / "scalp_bars.jsonl"):
        sig = str(rec.get("ob_signal") or "").lower()
        if sig not in ("long", "short"):
            continue
        ts = float(rec.get("t_close") or 0)
        if ts > best:
            best = ts
    return best


def last_trade_at(symbol: str) -> float:
    sym = symbol.upper()
    state = load_adaptive(sym)
    if state.last_trade_at > 0:
        return state.last_trade_at
    path = LOG_ROOT / sym / "scalp_trades.log"
    if not path.exists():
        return 0.0
    best = 0.0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            if not line.strip() or "MARKET" in line:
                continue
            if any(k in line for k in (" TP ", " SL ", " TRAIL ", " FLIP ", " MAXBARS ")):
                try:
                    ts_str = line[:19]
                    best = max(best, time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")))
                except (ValueError, OverflowError):
                    pass
    except OSError:
        pass
    return best


def last_entry_at(symbol: str) -> float:
    sym = symbol.upper()
    path = LOG_ROOT / sym / "scalp_trades.log"
    if not path.exists():
        return 0.0
    best = 0.0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            if "MARKET" not in line:
                continue
            try:
                ts_str = line[:19]
                best = max(best, time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")))
            except (ValueError, OverflowError):
                pass
    except OSError:
        pass
    return best


def stack_started_at(symbol: str) -> float:
    """When the active stack was last set for this symbol."""
    sym = symbol.upper()
    active = ROOT / ".run" / "scalp_active.json"
    if active.exists():
        try:
            data = json.loads(active.read_text(encoding="utf-8"))
            if str(data.get("symbol", "")).upper() == sym and data.get("updated_at"):
                return time.mktime(time.strptime(str(data["updated_at"]), "%Y-%m-%d %H:%M:%S"))
        except (OSError, json.JSONDecodeError, ValueError, OverflowError):
            pass
    ap = adaptive_path(sym)
    if ap.exists():
        try:
            mtime = ap.stat().st_mtime
            return mtime
        except OSError:
            pass
    return 0.0


def idle_minutes(symbol: str, *, mode: str = "signal") -> float:
    """Minutes since last activity. Uses stack start as floor (no false idle right after switch)."""
    sym = symbol.upper()
    now = time.time()
    floor = stack_started_at(sym) or now

    if mode == "trade":
        ref = last_trade_at(sym)
    elif mode == "entry":
        ref = last_entry_at(sym)
    else:
        ref = last_signal_at(sym)

    ref = max(ref, floor)
    if ref <= 0:
        return (now - floor) / 60.0 if floor > 0 else 0.0
    return max(0.0, (now - ref) / 60.0)


def activity_summary(symbol: str) -> dict[str, float]:
    sym = symbol.upper()
    return {
        "idle_signal_min": idle_minutes(sym, mode="signal"),
        "idle_trade_min": idle_minutes(sym, mode="trade"),
        "idle_entry_min": idle_minutes(sym, mode="entry"),
        "last_signal_at": last_signal_at(sym),
        "last_trade_at": last_trade_at(sym),
    }
