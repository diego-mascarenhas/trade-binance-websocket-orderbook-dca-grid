"""Per-symbol cooldown after a losing close.

Shared by orderbook_dca_grid (--supervise) and pump_stall_scan (--auto-trade).
State: .state/loss_cooldown.json
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

STATE_DIRNAME = ".state"
STATE_FILE = "loss_cooldown.json"


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _path() -> str:
    d = os.path.join(_repo_root(), STATE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, STATE_FILE)


def _load() -> dict[str, Any]:
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _prune(data: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    out: dict[str, Any] = {}
    for sym, row in data.items():
        if not isinstance(row, dict):
            continue
        try:
            until = float(row.get("until", 0) or 0)
        except (TypeError, ValueError):
            continue
        if until > now:
            out[str(sym).upper()] = row
    return out


def record_loss(
    symbol: str,
    pnl_usdt: float,
    cooldown_sec: float,
    *,
    reason: str | None = None,
) -> float:
    """Start/refresh cooldown for symbol. Returns until-epoch (0 if disabled)."""
    sec = max(0.0, float(cooldown_sec or 0))
    if sec <= 0:
        return 0.0
    sym = symbol.upper()
    now = time.time()
    until = now + sec
    data = _prune(_load(), now=now)
    data[sym] = {
        "until": until,
        "pnl_usdt": float(pnl_usdt),
        "at": now,
        "cooldown_sec": sec,
        "reason": reason or "loss",
    }
    _save(data)
    return until


def remaining_sec(symbol: str) -> float:
    sym = symbol.upper()
    now = time.time()
    data = _prune(_load(), now=now)
    if data != _load():
        _save(data)
    row = data.get(sym)
    if not row:
        return 0.0
    try:
        return max(0.0, float(row.get("until", 0) or 0) - now)
    except (TypeError, ValueError):
        return 0.0


def is_cooling(symbol: str) -> bool:
    return remaining_sec(symbol) > 0


def cooling_map() -> dict[str, float]:
    """symbol → seconds remaining for all active cooldowns."""
    now = time.time()
    data = _prune(_load(), now=now)
    _save(data)
    out: dict[str, float] = {}
    for sym, row in data.items():
        try:
            left = float(row.get("until", 0) or 0) - now
        except (TypeError, ValueError):
            continue
        if left > 0:
            out[sym] = left
    return out


def clear(symbol: str) -> bool:
    sym = symbol.upper()
    data = _prune(_load())
    if sym not in data:
        return False
    del data[sym]
    _save(data)
    return True


def fmt_remaining(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    mins, s = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m{s:02d}s" if s else f"{mins}m"
    hrs, m = divmod(mins, 60)
    return f"{hrs}h{m:02d}m"
