"""Auto-disable losing multi-trigger components / combo tags from journal PnL.

Scans ``.run/logs/*/scalp_trades.log``, aggregates PnL by trigger *component*
(e.g. ``ml``, ``htf``, ``bearish_engulfing``), and writes a disable list that
bots honor on the next entry decision.

Also blocks *exact* trigger combinations (full ``trigger=a+b+c`` tags) once
they accumulate enough losing closes (default: 1 loss).

Env:
  OB_TRIG_AUTO_DISABLE=1     master switch (default on)
  OB_TRIG_AUTO_MIN_N=15      min closes credited to a component
  OB_TRIG_AUTO_MAX_PNL=0     disable when sum PnL < this
  OB_TRIG_AUTO_MAX_WR=0.45   also require win-rate ≤ this (0=ignore WR)
  OB_TRIG_TAG_BLOCK=1        block exact combos after enough losses (default on)
  OB_TRIG_TAG_MAX_LOSSES=1   block when losses ≥ this (default 1)
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ob_candles import ALL_CANDLE_NAMES

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"
DISABLED_PATH = LOG_ROOT / "trig_disabled.json"

_CLOSE_RE = re.compile(
    r"(TP|SL|TRAIL|FLIP|MAXBARS) (LONG|SHORT) .*?"
    r"pnl=([+-]?[0-9.]+) USDT"
)
_TRIG_RE = re.compile(r"trigger=(\S+)")

# Journal component → enable key in collect_triggers (candles use their own name)
COMPONENT_ENABLE: dict[str, str] = {
    "momentum": "momentum",
    "imbalance": "imbalance",
    "ema_trend": "ema_trend",
    "ema_cross": "ema_cross",
    "htf": "htf",
    "pattern": "htf",  # legacy tag
    "ml": "ml",
    "choch": "choch",
    "eql": "eql",
    "eqh": "eqh",
    "rsi": "rsi",
    "stoch": "stoch",
}

# Never auto-disable (not real signal votes / noise)
SKIP_COMPONENTS = frozenset({"adopted", "unknown", ""})


@dataclass
class ComponentStats:
    name: str
    n: int
    wins: int
    losses: int
    pnl: float

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else 0.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_float(name, float(default)))
    except (TypeError, ValueError):
        return default


def auto_disable_enabled() -> bool:
    return _env_bool("OB_TRIG_AUTO_DISABLE", True)


def collect_component_stats(*, symbols: list[str] | None = None) -> dict[str, ComponentStats]:
    """Credit full close PnL to each part of trigger=a+b+c."""
    buckets: dict[str, list[float]] = defaultdict(list)
    paths: list[Path]
    if symbols:
        paths = [LOG_ROOT / s.upper() / "scalp_trades.log" for s in symbols]
    else:
        paths = list(LOG_ROOT.glob("*/scalp_trades.log")) if LOG_ROOT.exists() else []

    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _CLOSE_RE.search(line)
            if not m:
                continue
            pnl = float(m.group(3))
            tm = _TRIG_RE.search(line)
            tag = (tm.group(1) if tm else "unknown").strip()
            for part in tag.split("+"):
                part = part.strip() or "unknown"
                if part in SKIP_COMPONENTS:
                    continue
                # Normalize legacy
                if part == "pattern":
                    part = "htf"
                buckets[part].append(pnl)

    out: dict[str, ComponentStats] = {}
    for name, pnls in buckets.items():
        wins = sum(1 for p in pnls if p > 0)
        out[name] = ComponentStats(
            name=name,
            n=len(pnls),
            wins=wins,
            losses=len(pnls) - wins,
            pnl=sum(pnls),
        )
    return out


def evaluate_disabled(
    stats: dict[str, ComponentStats] | None = None,
    *,
    min_n: int | None = None,
    max_pnl: float | None = None,
    max_wr: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{component: {reason, n, pnl, wr}}`` that should be disabled."""
    stats = stats if stats is not None else collect_component_stats()
    min_n = _env_int("OB_TRIG_AUTO_MIN_N", 15) if min_n is None else min_n
    max_pnl = _env_float("OB_TRIG_AUTO_MAX_PNL", 0.0) if max_pnl is None else max_pnl
    max_wr = _env_float("OB_TRIG_AUTO_MAX_WR", 0.45) if max_wr is None else max_wr

    disabled: dict[str, dict[str, Any]] = {}
    for name, st in stats.items():
        if name in SKIP_COMPONENTS:
            continue
        if st.n < min_n:
            continue
        if st.pnl >= max_pnl:
            continue
        if max_wr > 0 and st.wr > max_wr:
            continue
        disabled[name] = {
            "n": st.n,
            "wins": st.wins,
            "losses": st.losses,
            "pnl": round(st.pnl, 4),
            "wr": round(st.wr, 3),
            "reason": f"n≥{min_n} pnl={st.pnl:+.4f}<{max_pnl:g} wr={st.wr:.0%}≤{max_wr:.0%}",
        }
    return disabled


def load_disabled() -> dict[str, dict[str, Any]]:
    if not DISABLED_PATH.exists():
        return {}
    try:
        raw = json.loads(DISABLED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    items = raw.get("disabled")
    return items if isinstance(items, dict) else {}


def disabled_names() -> set[str]:
    if not auto_disable_enabled():
        return set()
    return set(load_disabled().keys())


def refresh_trig_disabled(*, symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Recompute and persist disable list. Returns newly written disabled map."""
    if not auto_disable_enabled():
        if DISABLED_PATH.exists():
            # Keep file but mark inactive
            payload = {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "active": False,
                "disabled": {},
            }
            DISABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
            DISABLED_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {}

    stats = collect_component_stats(symbols=symbols)
    disabled = evaluate_disabled(stats)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active": True,
        "min_n": _env_int("OB_TRIG_AUTO_MIN_N", 15),
        "max_pnl": _env_float("OB_TRIG_AUTO_MAX_PNL", 0.0),
        "max_wr": _env_float("OB_TRIG_AUTO_MAX_WR", 0.45),
        "disabled": disabled,
        "watch": {
            k: {
                "n": v.n,
                "pnl": round(v.pnl, 4),
                "wr": round(v.wr, 3),
            }
            for k, v in sorted(stats.items(), key=lambda x: x[1].pnl)[:12]
        },
    }
    DISABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISABLED_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return disabled


def apply_disabled_to_enable(enable: dict[str, bool], disabled: set[str] | None = None) -> dict[str, bool]:
    """Flip enable keys off when their component is auto-disabled."""
    disabled = disabled if disabled is not None else disabled_names()
    out = dict(enable)
    for comp in disabled:
        if comp in ALL_CANDLE_NAMES:
            # candles stay on as a group; individual names filtered in collect_triggers
            continue
        key = COMPONENT_ENABLE.get(comp, comp)
        if key in out:
            out[key] = False
    # If every known candle name is disabled, turn candles group off
    if ALL_CANDLE_NAMES and ALL_CANDLE_NAMES <= disabled:
        out["candles"] = False
    return out


def format_disabled_summary(disabled: dict[str, dict[str, Any]] | None = None) -> str:
    disabled = disabled if disabled is not None else load_disabled()
    if not disabled:
        return "none"
    parts = [f"{k}({v.get('pnl', 0):+.2f}/{v.get('n', 0)})" for k, v in sorted(disabled.items())]
    return ", ".join(parts)


# ── Exact combo (full tag) loss filter ──────────────────────────────────────

_TAG_CACHE: dict[str, Any] = {"at": 0.0, "blocked": {}, "max_losses": 1}
_TAG_CACHE_TTL_S = 20.0


def tag_block_enabled() -> bool:
    return _env_bool("OB_TRIG_TAG_BLOCK", True)


def tag_max_losses() -> int:
    return max(1, _env_int("OB_TRIG_TAG_MAX_LOSSES", 1))


def normalize_trigger_tag(tag: str) -> str:
    """Normalize journal/live tags so ``pattern`` ≡ ``htf`` and order matches EntryDecision."""
    raw = (tag or "").strip()
    if not raw:
        return ""
    parts: list[str] = []
    for part in raw.split("+"):
        p = part.strip()
        if not p or p in SKIP_COMPONENTS:
            continue
        if p == "pattern":
            p = "htf"
        if p not in parts:
            parts.append(p)
    if not parts:
        return ""
    try:
        from ob_triggers import TRIGGER_PRIORITY

        parts = sorted(
            parts,
            key=lambda n: TRIGGER_PRIORITY.index(n) if n in TRIGGER_PRIORITY else 99,
        )
    except Exception:
        parts = sorted(parts)
    return "+".join(parts)


@dataclass
class TagStats:
    tag: str
    n: int
    wins: int
    losses: int
    pnl: float


def collect_tag_stats(*, symbols: list[str] | None = None) -> dict[str, TagStats]:
    """Aggregate closes by full normalized trigger tag (excludes adopted)."""
    buckets: dict[str, list[float]] = defaultdict(list)
    if symbols:
        paths = [LOG_ROOT / s.upper() / "scalp_trades.log" for s in symbols]
    else:
        paths = list(LOG_ROOT.glob("*/scalp_trades.log")) if LOG_ROOT.exists() else []

    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _CLOSE_RE.search(line)
            if not m:
                continue
            pnl = float(m.group(3))
            tm = _TRIG_RE.search(line)
            raw = (tm.group(1) if tm else "unknown").strip()
            if raw in SKIP_COMPONENTS or raw == "unknown":
                continue
            tag = normalize_trigger_tag(raw)
            if not tag:
                continue
            buckets[tag].append(pnl)

    out: dict[str, TagStats] = {}
    for tag, pnls in buckets.items():
        wins = sum(1 for p in pnls if p > 0)
        out[tag] = TagStats(
            tag=tag,
            n=len(pnls),
            wins=wins,
            losses=len(pnls) - wins,
            pnl=sum(pnls),
        )
    return out


def evaluate_blocked_tags(
    stats: dict[str, TagStats] | None = None,
    *,
    max_losses: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{tag: {wins, losses, pnl, n}}`` blocked for having ≥ max_losses losses."""
    stats = stats if stats is not None else collect_tag_stats()
    max_losses = tag_max_losses() if max_losses is None else max(1, int(max_losses))
    blocked: dict[str, dict[str, Any]] = {}
    for tag, st in stats.items():
        if st.losses < max_losses:
            continue
        blocked[tag] = {
            "n": st.n,
            "wins": st.wins,
            "losses": st.losses,
            "pnl": round(st.pnl, 4),
            "reason": f"losses≥{max_losses}",
        }
    return blocked


def _refresh_tag_cache(*, force: bool = False) -> dict[str, dict[str, Any]]:
    now = time.time()
    max_losses = tag_max_losses()
    if (
        not force
        and _TAG_CACHE["blocked"] is not None
        and _TAG_CACHE.get("max_losses") == max_losses
        and (now - float(_TAG_CACHE.get("at") or 0)) < _TAG_CACHE_TTL_S
    ):
        return _TAG_CACHE["blocked"]  # type: ignore[return-value]
    blocked = evaluate_blocked_tags(max_losses=max_losses) if tag_block_enabled() else {}
    _TAG_CACHE["at"] = now
    _TAG_CACHE["max_losses"] = max_losses
    _TAG_CACHE["blocked"] = blocked
    return blocked


def blocked_tag_map(*, force: bool = False) -> dict[str, dict[str, Any]]:
    if not tag_block_enabled():
        return {}
    return _refresh_tag_cache(force=force)


def is_tag_blocked(tag: str) -> bool:
    """True if this exact combo already has ≥ OB_TRIG_TAG_MAX_LOSSES losses."""
    if not tag_block_enabled():
        return False
    norm = normalize_trigger_tag(tag)
    if not norm:
        return False
    return norm in blocked_tag_map()


def format_blocked_tags_summary(blocked: dict[str, dict[str, Any]] | None = None) -> str:
    blocked = blocked if blocked is not None else blocked_tag_map(force=True)
    if not blocked:
        return "none"
    parts = [
        f"{k}({v.get('wins', 0)}W/{v.get('losses', 0)}L)"
        for k, v in sorted(blocked.items(), key=lambda x: -int(x[1].get("losses", 0)))
    ]
    return ", ".join(parts[:12]) + ("…" if len(parts) > 12 else "")
