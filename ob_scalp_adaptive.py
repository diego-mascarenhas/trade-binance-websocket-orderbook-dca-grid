"""Adaptive filters: permissive start, tighten on losses, relax when idle."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ob_scalp_ml import feature_vector

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"

# Minutes idle → relax filters (cumulative steps). Prefer more entries when quiet.
_INACTIVITY_STEPS: tuple[tuple[int, dict[str, float]], ...] = (
    (10, {"ml_min_prob": -0.04, "ema_slope_min": -0.005}),
    (20, {"ml_min_prob": -0.03, "imb_long_adj": -0.012, "imb_short_adj": 0.012}),
    (30, {"ml_min_prob": -0.03, "ema_slope_min": -0.005, "momentum_adj": -0.004}),
    (45, {"ml_min_prob": -0.03, "imb_long_adj": -0.010, "imb_short_adj": 0.010}),
    (60, {"ml_min_prob": -0.02, "momentum_adj": -0.003}),
)

# Floor keeps ML on (still blocks junk) but allows enough entries in chop.
_FLOORS = {
    "ml_min_prob": 0.15,
    "ema_slope_min": 0.005,
    "imb_long_adj": -0.06,
    "imb_short_adj": -0.02,
    "momentum_adj": -0.015,
    "sl_pct_adj": 0.0,
}

_SL_ADJ_CEILING = 0.15

_CEILINGS = {
    "ml_min_prob": 0.45,
    "ema_slope_min": 0.12,
    "imb_long_adj": 0.03,
    "imb_short_adj": 0.06,
    "momentum_adj": 0.02,
    "sl_pct_adj": _SL_ADJ_CEILING,
}


@dataclass
class AdaptiveState:
    ml_min_prob: float = 0.30
    ema_slope_min: float = 0.020
    imb_long_adj: float = -0.015
    imb_short_adj: float = 0.015
    momentum_adj: float = -0.003
    sl_pct_adj: float = 0.0
    last_trade_at: float = 0.0
    relax_steps: list[int] = field(default_factory=list)
    trades: int = 0
    wins: int = 0
    losses: int = 0
    updated_at: str = ""

    def touch(self) -> None:
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")


def adaptive_path(symbol: str) -> Path:
    p = LOG_ROOT / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "scalp_adaptive.json"


def trade_samples_path(symbol: str) -> Path:
    p = LOG_ROOT / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "scalp_trade_samples.jsonl"


def load_adaptive(symbol: str) -> AdaptiveState:
    path = adaptive_path(symbol)
    if not path.exists():
        return init_permissive(symbol)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return init_permissive(symbol)
    if not isinstance(raw, dict):
        return init_permissive(symbol)
    return AdaptiveState(
        ml_min_prob=float(raw.get("ml_min_prob", 0.30)),
        ema_slope_min=float(raw.get("ema_slope_min", 0.020)),
        imb_long_adj=float(raw.get("imb_long_adj", -0.015)),
        imb_short_adj=float(raw.get("imb_short_adj", 0.015)),
        momentum_adj=float(raw.get("momentum_adj", -0.003)),
        sl_pct_adj=float(raw.get("sl_pct_adj", 0) or 0),
        last_trade_at=float(raw.get("last_trade_at", 0) or 0),
        relax_steps=list(raw.get("relax_steps") or []),
        trades=int(raw.get("trades", 0) or 0),
        wins=int(raw.get("wins", 0) or 0),
        losses=int(raw.get("losses", 0) or 0),
        updated_at=str(raw.get("updated_at", "") or ""),
    )


def save_adaptive(symbol: str, state: AdaptiveState) -> None:
    state.touch()
    adaptive_path(symbol).write_text(
        json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def init_permissive(symbol: str) -> AdaptiveState:
    state = AdaptiveState()
    save_adaptive(symbol, state)
    return state


def _clamp(name: str, value: float) -> float:
    return max(_FLOORS[name], min(_CEILINGS[name], value))


def apply_delta(state: AdaptiveState, deltas: dict[str, float]) -> None:
    for key, delta in deltas.items():
        if not hasattr(state, key):
            continue
        if key == "sl_pct_adj":
            state.sl_pct_adj = max(0.0, min(_SL_ADJ_CEILING, state.sl_pct_adj + delta))
            continue
        setattr(state, key, _clamp(key, getattr(state, key) + delta))


def maybe_relax_inactivity(
    symbol: str,
    state: AdaptiveState,
    *,
    bot_started_at: float,
) -> str | None:
    """Apply inactivity relax steps. Returns log message if changed."""
    idle_min = (time.time() - max(state.last_trade_at, bot_started_at)) / 60.0
    changed = False
    msgs: list[str] = []
    for idx, (need_min, deltas) in enumerate(_INACTIVITY_STEPS):
        if idx in state.relax_steps:
            continue
        if idle_min < need_min:
            break
        apply_delta(state, deltas)
        state.relax_steps.append(idx)
        changed = True
        msgs.append(f"step{idx + 1}@{need_min}m")
    if not changed:
        return None
    save_adaptive(symbol, state)
    return f"Adaptive relax ({', '.join(msgs)}) idle={idle_min:.0f}m ml={state.ml_min_prob:.2f} ema={state.ema_slope_min:.3f}"


def on_trade_open(symbol: str, state: AdaptiveState) -> None:
    state.last_trade_at = time.time()
    save_adaptive(symbol, state)


def on_trade_close(
    symbol: str,
    state: AdaptiveState,
    *,
    signal: str,
    features: list[float],
    won: bool,
    net_usdt: float,
) -> str:
    """Immediate close feedback (used when post-close learn watch is off)."""
    state.last_trade_at = time.time()
    state.trades += 1
    if won:
        state.wins += 1
        # Mild tighten — do not starve entries after a win
        apply_delta(state, {"ml_min_prob": 0.005, "ema_slope_min": 0.001})
        if state.relax_steps:
            state.relax_steps.pop()
    else:
        state.losses += 1
        # Mild tighten — strong raises were blocking almost all signals
        apply_delta(state, {"ml_min_prob": 0.010, "ema_slope_min": 0.002})

    sample = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal": signal.lower(),
        "features": features,
        "label": 1 if won else 0,
        "net_usdt": round(net_usdt, 6),
    }
    with open(trade_samples_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    save_adaptive(symbol, state)
    outcome = "WIN" if won else "LOSS"
    return (
        f"Adaptive {outcome} pnl={net_usdt:+.4f} → ml={state.ml_min_prob:.2f} "
        f"ema={state.ema_slope_min:.3f} trades={state.trades} ({state.wins}W/{state.losses}L)"
    )


def effective_filters(
    state: AdaptiveState,
    *,
    base_ml: float,
    base_ema: float,
    base_imb_long: float,
    base_imb_short: float,
    base_momentum: float,
) -> dict[str, float]:
    # Use the looser of adaptive vs CLI base so ML stays on but does not starve entries.
    return {
        "ml_min_prob": min(state.ml_min_prob, base_ml),
        "ema_slope_min": min(state.ema_slope_min, base_ema),
        "imb_long": max(0.50, base_imb_long + state.imb_long_adj),
        "imb_short": min(0.50, base_imb_short + state.imb_short_adj),
        "momentum_min_pct": max(0.003, base_momentum + state.momentum_adj),
    }


def format_adaptive_line(state: AdaptiveState, eff: dict[str, float], *, base_sl: float = 0.12) -> str:
    idle = (time.time() - state.last_trade_at) / 60.0 if state.last_trade_at else 0.0
    sl_note = ""
    if state.sl_pct_adj > 0:
        sl_note = f" sl={base_sl + state.sl_pct_adj:.3f}%"
    return (
        f"adaptive ml≥{eff['ml_min_prob']:.2f} ema≥{eff['ema_slope_min']:.3f}% "
        f"imb {eff['imb_long']:.3f}/{eff['imb_short']:.3f} "
        f"mom≥{eff['momentum_min_pct']:.3f}%{sl_note} · idle {idle:.0f}m · relax {len(state.relax_steps)}"
    )


def load_trade_samples(symbol: str, *, limit: int = 200) -> list[dict[str, Any]]:
    path = trade_samples_path(symbol)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def features_from_bar_record(rec) -> list[float]:
    return feature_vector(rec)
