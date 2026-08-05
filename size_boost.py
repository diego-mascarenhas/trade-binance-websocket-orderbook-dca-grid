"""Per-symbol entry/grid size boost via `.state/boost/SYMBOL.json`.

Used at arm time: ``base_size *= mult`` (entry + DCA ladder).

Sources:
  * ``manual`` — Telegram ``/boost`` (never overwritten by auto)
  * ``auto`` — scanner: strict-eligible ★ + dwell ≥ N cycles + only #1

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
DWELL_FILENAME = "boost_dwell.json"
DEFAULT_MULT = 1.5
MIN_MULT = 1.0
MAX_MULT = 5.0
DEFAULT_AUTO_TTL_H = 5.0
DEFAULT_AUTO_DWELL = 2

# Strict profile thresholds (defaults of ./pump-stall-watch / argparse)
STRICT_MIN_STALL = 35.0
STRICT_MIN_NEAR = 85.0
STRICT_IDEAL_NEAR = 92.0
STRICT_MIN_REGIME = 80.0
STRICT_MIN_SHARP = 35.0
STRICT_MIN_PUMP_7D = 35.0

_SYM_RE = re.compile(r"^[A-Z0-9]{4,32}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def boost_dir() -> Path:
    d = _repo_root() / STATE_DIRNAME / BOOST_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def boost_path(symbol: str) -> Path:
    return boost_dir() / f"{symbol.upper()}.json"


def dwell_path() -> Path:
    return _repo_root() / STATE_DIRNAME / DWELL_FILENAME


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def default_mult() -> float:
    return _clamp(_env_float("SIZE_BOOST_DEFAULT", DEFAULT_MULT))


def max_mult() -> float:
    return max(MIN_MULT, _env_float("SIZE_BOOST_MAX", MAX_MULT))


def auto_enabled() -> bool:
    return _env_bool("SIZE_BOOST_AUTO", True)


def auto_ttl_hours() -> float:
    return max(0.25, _env_float("SIZE_BOOST_AUTO_TTL_H", DEFAULT_AUTO_TTL_H))


def auto_dwell_cycles() -> int:
    return max(1, _env_int("SIZE_BOOST_AUTO_DWELL", DEFAULT_AUTO_DWELL))


def _clamp(mult: float) -> float:
    return max(MIN_MULT, min(max_mult(), float(mult)))


def normalize_symbol(symbol: str) -> str | None:
    sym = (symbol or "").strip().upper()
    if not sym or not _SYM_RE.match(sym):
        return None
    return sym


def _read_row(symbol: str) -> dict[str, Any] | None:
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
    return data if isinstance(data, dict) else None


def _until_expired(until: Any) -> bool:
    if until is None or until == "":
        return False
    try:
        if isinstance(until, (int, float)):
            exp = float(until)
        else:
            from datetime import datetime, timezone

            exp = datetime.fromisoformat(str(until).replace("Z", "+00:00")).timestamp()
        return time.time() >= exp
    except (TypeError, ValueError, OSError):
        return False


def get_mult(symbol: str) -> float | None:
    """Return active boost multiplier, or None if no file / invalid / expired."""
    data = _read_row(symbol)
    if not data:
        return None
    sym = normalize_symbol(symbol)
    if data.get("until") is not None and _until_expired(data.get("until")):
        if sym:
            clear(sym)
        return None
    try:
        mult = float(data.get("mult", 0) or 0)
    except (TypeError, ValueError):
        return None
    if mult < MIN_MULT:
        return None
    return _clamp(mult)


def boost_source(symbol: str) -> str | None:
    """``manual``, ``auto``, or None if no active boost."""
    if get_mult(symbol) is None:
        return None
    data = _read_row(symbol) or {}
    src = str(data.get("source") or "manual").strip().lower()
    return "auto" if src == "auto" else "manual"


def is_manual(symbol: str) -> bool:
    return boost_source(symbol) == "manual"


def apply_boost(symbol: str, base_size: float) -> tuple[float, float | None]:
    """Return (sized, mult_or_None)."""
    size = float(base_size or 0)
    if size <= 0:
        return size, None
    mult = get_mult(symbol)
    if mult is None:
        return size, None
    return size * mult, mult


def set_boost(
    symbol: str,
    mult: float | None = None,
    *,
    source: str = "manual",
    reason: str | None = None,
    ttl_hours: float | None = None,
) -> dict[str, Any]:
    """Create/update boost file. ``mult=None`` → default (1.5)."""
    sym = normalize_symbol(symbol)
    if not sym:
        raise ValueError("Invalid symbol")
    m = _clamp(default_mult() if mult is None else float(mult))
    src = "auto" if str(source).strip().lower() == "auto" else "manual"
    row: dict[str, Any] = {
        "symbol": sym,
        "mult": m,
        "source": src,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if reason:
        row["reason"] = str(reason)[:200]
    if ttl_hours is not None and float(ttl_hours) > 0:
        row["until"] = time.time() + float(ttl_hours) * 3600.0
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


def clear_auto(symbol: str) -> bool:
    """Clear only if source is auto (manual left alone)."""
    if boost_source(symbol) != "auto":
        return False
    return clear(symbol)


def list_boosts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = boost_dir()
    for path in sorted(d.glob("*.json")):
        sym = path.stem.upper()
        mult = get_mult(sym)
        if mult is None:
            continue
        data = _read_row(sym) or {}
        out.append({
            "symbol": sym,
            "mult": mult,
            "source": boost_source(sym) or "manual",
            "reason": data.get("reason"),
            "until": data.get("until"),
            "path": str(path),
        })
    return out


def fmt_mult(mult: float) -> str:
    return f"{mult:g}×"


def is_strict_eligible(hit: Any) -> bool:
    """True if metrics would pass the strict scanner + ★ ideal."""
    try:
        stall = float(getattr(hit, "stall_score", 0) or 0)
        near = float(getattr(hit, "near_high_pct", 0) or 0)
        regime = float(getattr(hit, "near_regime_pct", 0) or 0)
        sharp = float(getattr(hit, "sharp_pct", 0) or 0)
        pump = float(getattr(hit, "pump_7d_pct", 0) or 0)
    except (TypeError, ValueError):
        return False
    stall_min = _env_float("SIZE_BOOST_STRICT_STALL", STRICT_MIN_STALL)
    near_min = _env_float("SIZE_BOOST_STRICT_NEAR", STRICT_MIN_NEAR)
    ideal = _env_float("SIZE_BOOST_STRICT_IDEAL", STRICT_IDEAL_NEAR)
    regime_min = _env_float("SIZE_BOOST_STRICT_REGIME", STRICT_MIN_REGIME)
    sharp_min = _env_float("SIZE_BOOST_STRICT_SHARP", STRICT_MIN_SHARP)
    pump_min = _env_float("SIZE_BOOST_STRICT_PUMP", STRICT_MIN_PUMP_7D)
    return (
        stall >= stall_min
        and near >= near_min
        and near >= ideal
        and regime >= regime_min
        and sharp >= sharp_min
        and pump >= pump_min
    )


def update_star_dwell(star_symbols: list[str]) -> dict[str, int]:
    """Bump consecutive ★ cycles; reset symbols that left the ★ set."""
    stars = {s.upper() for s in star_symbols if normalize_symbol(s)}
    path = dwell_path()
    prev: dict[str, int] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        prev[str(k).upper()] = int(v)
                    except (TypeError, ValueError):
                        continue
        except (OSError, json.JSONDecodeError):
            prev = {}
    next_map: dict[str, int] = {}
    for sym in stars:
        next_map[sym] = int(prev.get(sym, 0)) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(next_map, indent=2) + "\n", encoding="utf-8")
    return next_map


def _clear_stale_auto(keep: str | None) -> list[str]:
    """Remove auto boosts except ``keep`` (and never touch manual)."""
    cleared: list[str] = []
    keep_u = (keep or "").upper() or None
    for row in list_boosts():
        if row.get("source") != "auto":
            continue
        sym = str(row["symbol"]).upper()
        if keep_u and sym == keep_u:
            continue
        if clear_auto(sym):
            cleared.append(sym)
    return cleared


def sync_auto_boost(
    hits: list[Any],
    *,
    ideal_near: float,
) -> str | None:
    """Update dwell + auto-boost #1 strict-eligible ★ with enough dwell.

    Returns a short status line for logs, or None if auto disabled / quiet.
    """
    if not auto_enabled():
        return None

    stars = [
        h for h in hits
        if float(getattr(h, "near_high_pct", 0) or 0) >= float(ideal_near)
    ]
    star_syms = [str(getattr(h, "symbol", "")).upper() for h in stars]
    dwell = update_star_dwell(star_syms)
    need = auto_dwell_cycles()

    candidates: list[Any] = []
    for h in stars:
        sym = str(getattr(h, "symbol", "")).upper()
        if not normalize_symbol(sym):
            continue
        if not is_strict_eligible(h):
            continue
        if int(dwell.get(sym, 0)) < need:
            continue
        candidates.append(h)

    candidates.sort(key=lambda h: float(getattr(h, "score", 0) or 0), reverse=True)
    top = candidates[0] if candidates else None

    if top is None:
        cleared = _clear_stale_auto(None)
        if cleared:
            return f"AUTO boost cleared (no strict ★ dwell≥{need}): {', '.join(cleared)}"
        return None

    sym = str(top.symbol).upper()
    if is_manual(sym):
        cleared = _clear_stale_auto(sym)
        bits = [f"{sym} manual (auto skipped)"]
        if cleared:
            bits.append(f"cleared {', '.join(cleared)}")
        return "AUTO boost: " + " · ".join(bits)

    stall = float(getattr(top, "stall_score", 0) or 0)
    near = float(getattr(top, "near_high_pct", 0) or 0)
    score = float(getattr(top, "score", 0) or 0)
    cycles = int(dwell.get(sym, 0))
    reason = (
        f"auto strict★ #{1} score={score:.1f} stall={stall:.0f} "
        f"near={near:.0f}% dwell={cycles}"
    )
    row = set_boost(
        sym,
        default_mult(),
        source="auto",
        reason=reason,
        ttl_hours=auto_ttl_hours(),
    )
    cleared = _clear_stale_auto(sym)
    msg = (
        f"AUTO boost {sym} → {fmt_mult(float(row['mult']))} "
        f"(strict★ dwell={cycles}/{need}, TTL {auto_ttl_hours():g}h)"
    )
    if cleared:
        msg += f" · cleared {', '.join(cleared)}"
    return msg
