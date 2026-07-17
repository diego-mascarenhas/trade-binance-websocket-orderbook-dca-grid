"""Multi-trigger entry helpers for OB scalp.

Triggers are evaluated independently (OR). Each firing source is tagged so
trades can be compared later in ./obscalp-trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ob_bars import OBBar
from ob_candles import ALL_CANDLE_NAMES, candle_side
from ob_ema import EmaSnapshot
from ob_oscillators import OscillatorSnapshot
from ob_pattern import PatternSnapshot
from ob_structure import StructureSnapshot
from ob_signals import SignalConfig, entry_signal
from ob_trig_learn import apply_disabled_to_enable, disabled_names

# Display / priority order when several fire together
TRIGGER_PRIORITY = (
    "choch",
    "eql",
    "eqh",
    "rsi",
    "stoch",
    "htf",
    *sorted(ALL_CANDLE_NAMES),
    "ema_cross",
    "ema_trend",
    "imbalance",
    "momentum",
    "pattern",  # legacy alias (maps to htf in collect)
    "ml",
)


@dataclass
class TriggerHit:
    side: str  # long | short
    name: str


@dataclass
class EntryDecision:
    side: str | None
    triggers: list[str]

    @property
    def tag(self) -> str:
        if not self.triggers:
            return ""
        ordered = sorted(
            self.triggers,
            key=lambda n: TRIGGER_PRIORITY.index(n) if n in TRIGGER_PRIORITY else 99,
        )
        return "+".join(ordered)


def _momentum_side(bar: OBBar, cfg: SignalConfig) -> str | None:
    ch = bar.mid_change_pct()
    if ch >= cfg.momentum_min_pct:
        return "long"
    if ch <= -cfg.momentum_min_pct:
        return "short"
    return None


def _imbalance_side(bar: OBBar, cfg: SignalConfig) -> str | None:
    if bar.imbalance >= cfg.imb_long:
        if cfg.require_momentum and bar.mid_change_pct() < cfg.momentum_min_pct:
            return None
        return "long"
    if bar.imbalance <= cfg.imb_short:
        if cfg.require_momentum and bar.mid_change_pct() > -cfg.momentum_min_pct:
            return None
        return "short"
    return None


def collect_triggers(
    bar: OBBar,
    cfg: SignalConfig,
    *,
    ema: EmaSnapshot | None = None,
    pattern: PatternSnapshot | None = None,
    structure: StructureSnapshot | None = None,
    oscillators: OscillatorSnapshot | None = None,
    ml_prob_long: float | None = None,
    ml_prob_short: float | None = None,
    ml_min_prob: float = 0.20,
    min_hits: int = 1,
    enable: dict[str, bool] | None = None,
) -> EntryDecision:
    """OR across enabled triggers; prefer the side with more / higher-priority hits.

    ``min_hits`` requires at least that many agreeing triggers on the chosen side
    (default 1 = classic OR). Use 2+ to cut weak single-source entries.
    """
    on = {
        "momentum": True,
        "imbalance": True,
        "ema_trend": True,
        "ema_cross": True,
        "htf": True,
        "candles": True,
        "ml": True,
        "ichocho": True,
        "choch": True,
        "eql": True,
        "eqh": True,
        "rsi": True,
        "stoch": True,
        **(enable or {}),
    }
    # Legacy enable key "pattern" → htf
    if "pattern" in (enable or {}):
        on["htf"] = bool(enable.get("pattern"))  # type: ignore[union-attr]
    banned = disabled_names()
    on = apply_disabled_to_enable(on, banned)
    hits: list[TriggerHit] = []

    if on.get("momentum", True) and "momentum" not in banned:
        side = _momentum_side(bar, cfg)
        if side:
            hits.append(TriggerHit(side, "momentum"))

    if on.get("imbalance", True) and "imbalance" not in banned:
        side = _imbalance_side(bar, cfg)
        if side:
            hits.append(TriggerHit(side, "imbalance"))

    if ema is not None:
        if on.get("ema_cross", True) and "ema_cross" not in banned:
            if getattr(ema, "cross_up", False):
                hits.append(TriggerHit("long", "ema_cross"))
            if getattr(ema, "cross_down", False):
                hits.append(TriggerHit("short", "ema_cross"))
        if on.get("ema_trend", True) and "ema_trend" not in banned:
            if ema.allow_long:
                hits.append(TriggerHit("long", "ema_trend"))
            if ema.allow_short:
                hits.append(TriggerHit("short", "ema_trend"))

    if pattern is not None:
        if on.get("htf", True) and "htf" not in banned and "pattern" not in banned:
            if pattern.allow_long:
                hits.append(TriggerHit("long", "htf"))
            if pattern.allow_short:
                hits.append(TriggerHit("short", "htf"))
        if on.get("candles", True):
            for name in getattr(pattern, "candles", None) or []:
                if name in banned:
                    continue
                side = candle_side(name)
                if side in ("long", "short"):
                    hits.append(TriggerHit(side, name))

    if structure is not None:
        choch_on = (on.get("choch", True) or on.get("ichocho", True)) and "choch" not in banned
        if choch_on and structure.choch in ("long", "short"):
            hits.append(TriggerHit(structure.choch, "choch"))
        if on.get("eql", True) and "eql" not in banned and structure.eql:
            hits.append(TriggerHit("long", "eql"))
        if on.get("eqh", True) and "eqh" not in banned and structure.eqh:
            hits.append(TriggerHit("short", "eqh"))

    if oscillators is not None:
        if on.get("rsi", True) and "rsi" not in banned and oscillators.rsi_side in ("long", "short"):
            hits.append(TriggerHit(oscillators.rsi_side, "rsi"))
        if on.get("stoch", True) and "stoch" not in banned and oscillators.stoch_side in ("long", "short"):
            hits.append(TriggerHit(oscillators.stoch_side, "stoch"))

    if on.get("ml", True) and "ml" not in banned:
        if ml_prob_long is not None and ml_prob_long >= ml_min_prob:
            hits.append(TriggerHit("long", "ml"))
        if ml_prob_short is not None and ml_prob_short >= ml_min_prob:
            hits.append(TriggerHit("short", "ml"))

    need = max(1, int(min_hits))
    if not hits:
        if need > 1:
            return EntryDecision(side=None, triggers=[])
        # Fallback: legacy single entry_signal if configured for imbalance-only path
        legacy = entry_signal(bar, cfg)
        if legacy:
            return EntryDecision(side=legacy, triggers=["momentum" if not cfg.use_imbalance else "imbalance"])
        return EntryDecision(side=None, triggers=[])

    long_hits = [h.name for h in hits if h.side == "long"]
    short_hits = [h.name for h in hits if h.side == "short"]

    def score(names: list[str]) -> tuple[int, int]:
        # more triggers wins; then better priority (lower index)
        pri = min((TRIGGER_PRIORITY.index(n) for n in names if n in TRIGGER_PRIORITY), default=99)
        return (len(names), -pri)

    chosen: EntryDecision
    if long_hits and not short_hits:
        chosen = EntryDecision(side="long", triggers=sorted(set(long_hits)))
    elif short_hits and not long_hits:
        chosen = EntryDecision(side="short", triggers=sorted(set(short_hits)))
    elif score(long_hits) >= score(short_hits):
        chosen = EntryDecision(side="long", triggers=sorted(set(long_hits)))
    else:
        chosen = EntryDecision(side="short", triggers=sorted(set(short_hits)))

    if chosen.side and len(chosen.triggers) < need:
        return EntryDecision(side=None, triggers=[])
    return chosen


@dataclass
class ObExitLevels:
    tp_price: float
    sl_price: float
    tp_on_wall: bool
    sl_on_wall: bool
    tp_dist_pct: float
    sl_dist_pct: float
    note: str


def resolve_ob_exits(
    bids: list[list[float]],
    asks: list[list[float]],
    entry: float,
    is_long: bool,
    *,
    fee_buffer: float,
    tick: Decimal,
    tp_pct_fallback: float,
    sl_pct_fallback: float,
    wall_min_mult: float = 1.0,
    min_dist_pct: float | None = None,
    max_range_pct: float = 3.0,
) -> ObExitLevels:
    """TP on opposing book wall, SL on supporting wall; % fallback if needed."""
    from orderbook_dca_grid import choose_tp_activation, select_walls

    min_dist = fee_buffer if min_dist_pct is None else min_dist_pct
    # Round-trip fee floor: wall must clear fees with margin (fee_buffer alone is too tight).
    min_tp_pct = max(fee_buffer * 2.0, fee_buffer + 0.15, 0.30)

    # TP: opposing wall (callback=0 → activation ≈ wall / fee floor)
    tp_info = choose_tp_activation(
        bids, asks, entry, entry, is_long,
        callback=0.0,
        fee_buffer=fee_buffer,
        tick=tick,
        wall_min_mult=wall_min_mult,
        pick="nearest",
    )
    tp_price = float(tp_info["activation"])
    tp_on_wall = bool(tp_info.get("on_wall"))
    tp_dist = abs(tp_price - entry) / entry * 100 if entry > 0 else 0.0
    if tp_dist < min_tp_pct or (max_range_pct > 0 and tp_dist > max_range_pct):
        use_pct = max(tp_pct_fallback, min_tp_pct)
        if is_long:
            tp_price = entry * (1 + use_pct / 100)
        else:
            tp_price = entry * (1 - use_pct / 100)
        tp_on_wall = False
        tp_dist = use_pct

    # SL: supporting wall via select_walls
    levels = bids if is_long else asks
    walls = select_walls(
        levels, entry, is_long,
        count=1,
        min_gap_pct=0.05,
        min_dist_pct=min_dist,
        max_range_pct=max_range_pct if max_range_pct > 0 else 12.0,
    )
    sl_on_wall = bool(walls)
    if walls:
        sl_price = float(walls[0][0])
        sl_dist = float(walls[0][2])
    else:
        if is_long:
            sl_price = entry * (1 - sl_pct_fallback / 100)
        else:
            sl_price = entry * (1 + sl_pct_fallback / 100)
        sl_dist = sl_pct_fallback
        sl_on_wall = False

    # Hard clamp: SL must stay beyond fee buffer
    if sl_dist < fee_buffer:
        if is_long:
            sl_price = entry * (1 - max(sl_pct_fallback, fee_buffer) / 100)
        else:
            sl_price = entry * (1 + max(sl_pct_fallback, fee_buffer) / 100)
        sl_dist = abs(sl_price - entry) / entry * 100
        sl_on_wall = False

    note = (
        f"TP {'wall' if tp_on_wall else '%'} {tp_dist:.2f}% · "
        f"SL {'wall' if sl_on_wall else '%'} {sl_dist:.2f}%"
    )
    return ObExitLevels(
        tp_price=tp_price,
        sl_price=sl_price,
        tp_on_wall=tp_on_wall,
        sl_on_wall=sl_on_wall,
        tp_dist_pct=tp_dist,
        sl_dist_pct=sl_dist,
        note=note,
    )


def hit_tp(mark: float, is_long: bool, tp_price: float) -> bool:
    if tp_price <= 0:
        return False
    return mark >= tp_price if is_long else mark <= tp_price


def hit_sl(mark: float, is_long: bool, sl_price: float) -> bool:
    if sl_price <= 0:
        return False
    return mark <= sl_price if is_long else mark >= sl_price
