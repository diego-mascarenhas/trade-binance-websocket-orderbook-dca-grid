"""Per-symbol entry/grid size boost via `.state/boost/SYMBOL.json`.

Used at arm time: ``base_size *= mult`` (entry + DCA ladder).
Manage with Telegram ``/boost`` or by creating/deleting the JSON file.

Default mult when enabling without a number: 1.5
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

STATE_DIRNAME = ".state"
BOOST_DIRNAME = "boost"
DEFAULT_MULT = 1.5
MIN_MULT = 1.0
MAX_MULT = 5.0

_SYM_RE = re.compile(r"^[A-Z0-9]{4,32}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def boost_dir() -> Path:
    d = _repo_root() / STATE_DIRNAME / BOOST_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def boost_path(symbol: str) -> Path:
    return boost_dir() / f"{symbol.upper()}.json"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def default_mult() -> float:
    return _clamp(_env_float("SIZE_BOOST_DEFAULT", DEFAULT_MULT))


def max_mult() -> float:
    return max(MIN_MULT, _env_float("SIZE_BOOST_MAX", MAX_MULT))


def _clamp(mult: float) -> float:
    return max(MIN_MULT, min(max_mult(), float(mult)))


def normalize_symbol(symbol: str) -> str | None:
    sym = (symbol or "").strip().upper()
    if not sym or not _SYM_RE.match(sym):
        return None
    return sym


def get_mult(symbol: str) -> float | None:
    """Return active boost multiplier, or None if no file / invalid."""
    sym = normalize_symbol(symbol)
    if not sym:
        return None
    path = boost_path(sym)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Optional expiry
    until = data.get("until")
    if until:
        try:
            # unix ts or ISO
            if isinstance(until, (int, float)):
                exp = float(until)
            else:
                from datetime import datetime, timezone

                exp = datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp()
            if time.time() >= exp:
                clear(sym)
                return None
        except (TypeError, ValueError, OSError):
            pass
    try:
        mult = float(data.get("mult", 0) or 0)
    except (TypeError, ValueError):
        return None
    if mult < MIN_MULT:
        return None
    return _clamp(mult)


def apply_boost(symbol: str, base_size: float) -> tuple[float, float | None]:
    """Return (sized, mult_or_None)."""
    size = float(base_size or 0)
    if size <= 0:
        return size, None
    mult = get_mult(symbol)
    if mult is None:
        return size, None
    return size * mult, mult


def set_boost(symbol: str, mult: float | None = None) -> dict[str, Any]:
    """Create/update boost file. ``mult=None`` → default (1.5)."""
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Invalid symbol")
    m = _clamp(default_mult() if mult is None else float(mult))
    row = {
        "symbol": sym,
        "mult": m,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = boost_path(sym)
    path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return row


def clear(symbol: str) -> bool:
    sym = normalize_symbol(symbol)
    if not sym:
        return False
    path = boost_path(sym)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def list_boosts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = boost_dir()
    for path in sorted(d.glob("*.json")):
        sym = path.stem.upper()
        mult = get_mult(sym)
        if mult is None:
            continue
        out.append({"symbol": sym, "mult": mult, "path": str(path)})
    return out


def fmt_mult(mult: float) -> str:
    return f"{mult:g}×"
