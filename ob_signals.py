"""Entry/exit signals from synthetic order-book bars."""

from __future__ import annotations

from dataclasses import dataclass

from ob_bars import OBBar


@dataclass
class SignalConfig:
    imb_long: float = 0.55
    imb_short: float = 0.45
    min_wall_qty: float = 0.0
    require_momentum: bool = True
    momentum_min_pct: float = 0.01


def entry_signal(bar: OBBar, cfg: SignalConfig) -> str | None:
    """Return 'long', 'short', or None at bar close."""
    if bar.imbalance >= cfg.imb_long:
        if cfg.min_wall_qty > 0 and bar.bid_wall_qty < cfg.min_wall_qty:
            return None
        if cfg.require_momentum and bar.mid_change_pct() < cfg.momentum_min_pct:
            return None
        return "long"

    if bar.imbalance <= cfg.imb_short:
        if cfg.min_wall_qty > 0 and bar.ask_wall_qty < cfg.min_wall_qty:
            return None
        if cfg.require_momentum and bar.mid_change_pct() > -cfg.momentum_min_pct:
            return None
        return "short"

    return None


def exit_on_flip(is_long: bool, bar: OBBar, cfg: SignalConfig) -> bool:
    """Exit when book imbalance flips against the open side."""
    if is_long:
        return bar.imbalance <= cfg.imb_short
    return bar.imbalance >= cfg.imb_long


def profit_pct(entry: float, mark: float, is_long: bool) -> float:
    if entry <= 0 or mark <= 0:
        return 0.0
    if is_long:
        return (mark - entry) / entry * 100
    return (entry - mark) / entry * 100


def estimated_net_pct(gross_pct: float, fee_buffer_pct: float) -> float:
    """Rough net %% after round-trip taker fees (entry + exit market)."""
    return gross_pct - fee_buffer_pct


def should_tp_close(gross_pct: float, tp_pct: float, fee_buffer_pct: float) -> bool:
    """TP only when gross target hit and estimated net stays positive."""
    return gross_pct >= tp_pct and estimated_net_pct(gross_pct, fee_buffer_pct) > 0


def should_discretionary_close(gross_pct: float, fee_buffer_pct: float) -> bool:
    """Flip / time exit — skip if estimated net would be zero or negative."""
    return estimated_net_pct(gross_pct, fee_buffer_pct) > 0
