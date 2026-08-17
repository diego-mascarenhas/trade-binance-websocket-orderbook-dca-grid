"""★ rising-edge pulse so a frozen 12× position can place one more DCA grid.

Watch writes a token when a symbol becomes ★ again while a supervisor is
already running. The supervisor consumes it on a successful DCA-only place
and marks the new grid active so the 12× cap does not cancel it.

The first time we see a live supervisor already ★ (deploy / restart), we
inherit that episode and do **not** pulse — “again” means it left ★ and
came back.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".state"
SEEN_DIRNAME = "star_seen"
REARM_DIRNAME = "star_rearm"
ACTIVE_DIRNAME = "star_grid"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _dir(name: str) -> Path:
    d = _repo_root() / STATE_DIRNAME / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str, symbol: str) -> Path:
    return _dir(name) / f"{symbol.upper()}.json"


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def pending(symbol: str) -> bool:
    return _path(REARM_DIRNAME, symbol).exists()


def grid_active(symbol: str) -> bool:
    return _path(ACTIVE_DIRNAME, symbol).exists()


def consume(symbol: str) -> bool:
    path = _path(REARM_DIRNAME, symbol)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def mark_grid_active(symbol: str) -> None:
    _write(
        _path(ACTIVE_DIRNAME, symbol),
        {"symbol": symbol.upper(), "ts": time.time()},
    )


def clear_grid_active(symbol: str) -> None:
    path = _path(ACTIVE_DIRNAME, symbol)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def note_grid_empty(symbol: str, has_dca: bool) -> None:
    """Drop the star-grid shield once those safety orders are gone."""
    if has_dca:
        return
    if grid_active(symbol):
        clear_grid_active(symbol)


def _seen_star(symbol: str) -> bool | None:
    row = _read(_path(SEEN_DIRNAME, symbol))
    if row is None:
        return None
    return bool(row.get("star"))


def _set_seen(symbol: str, is_star: bool) -> None:
    _write(
        _path(SEEN_DIRNAME, symbol),
        {"symbol": symbol.upper(), "star": bool(is_star), "ts": time.time()},
    )


def _pulse(symbol: str) -> bool:
    path = _path(REARM_DIRNAME, symbol)
    if path.exists():
        return False
    _write(
        path,
        {"symbol": symbol.upper(), "ts": time.time(), "reason": "star_again"},
    )
    return True


def sync_from_scan(
    hits: list[Any],
    ideal_near: float,
    running: set[str],
) -> str | None:
    """Rising-edge ★ on an already-supervised symbol → one re-arm token."""
    now_stars = {
        str(getattr(h, "symbol", "") or "").upper()
        for h in hits
        if float(getattr(h, "near_high_pct", 0) or 0) >= float(ideal_near)
    }
    now_stars.discard("")
    running_u = {s.upper() for s in running}
    pulsed: list[str] = []

    seen_dir = _dir(SEEN_DIRNAME)
    known = {p.stem.upper() for p in seen_dir.glob("*.json")}

    # Running but not ★: remember "off" so the next ★ is a real rising edge.
    # (If we leave seen=None, the next ★ would be treated as inherit / no pulse.)
    for sym in sorted(running_u - now_stars):
        if _seen_star(sym) is None:
            _set_seen(sym, False)

    for sym in sorted(now_stars):
        was = _seen_star(sym)
        if was is None:
            # Already ★ at first sight while supervised → same episode, no pulse.
            _set_seen(sym, True)
            continue
        if was:
            _set_seen(sym, True)
            continue
        _set_seen(sym, True)
        if sym in running_u and _pulse(sym):
            pulsed.append(sym)

    for sym in sorted(known - now_stars):
        if _seen_star(sym):
            _set_seen(sym, False)

    if not pulsed:
        return None
    return "★ re-arm pulse: " + ", ".join(pulsed)
