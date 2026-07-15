"""Staged exit: TP1 partial @ profit target, SL runner @ entry+BE_PROFIT_PCT, trailing wall."""

from __future__ import annotations

import argparse
from decimal import Decimal
from types import SimpleNamespace


def _staged_args(grid_args: argparse.Namespace) -> argparse.Namespace:
    import orderbook_staged_exit as staged

    direction = getattr(grid_args, "direction", "auto")
    dir_pin = direction if direction in ("long", "short") else None
    return SimpleNamespace(
        symbol=grid_args.symbol,
        dry_run=not getattr(grid_args, "execute", True),
        direction=dir_pin,
        tp1_profit_pct=(
            grid_args.tp1_profit_pct
            if getattr(grid_args, "tp1_profit_pct", None) is not None
            else staged._env_float("TP1_PROFIT_PCT", 0.3)
        ),
        be_profit_pct=(
            grid_args.be_profit_pct
            if getattr(grid_args, "be_profit_pct", None) is not None
            else staged._env_float("BE_PROFIT_PCT", 0.1)
        ),
        tp_partial_pct=(
            grid_args.tp_partial_pct
            if getattr(grid_args, "tp_partial_pct", None) is not None
            else staged._env_float("TP_PARTIAL_PCT", 70.0)
        ),
        tp_callback=getattr(grid_args, "tp_callback", staged._env_float("TP_CALLBACK", 0.2)),
        tp_fee_buffer=getattr(
            grid_args, "tp_fee_buffer", staged._env_float("TP_FEE_BUFFER", 0.12),
        ),
        tp_wall_min_mult=getattr(grid_args, "tp_wall_min_mult", 3.0),
        tp_wall_pick=getattr(grid_args, "tp_wall_pick", "nearest"),
        sl_pct=staged._env_float("SL_PCT", 2.0),
        sl_wall=False,
        cancel_dca=False,
        limit=getattr(grid_args, "limit", 100),
        poll_sec=staged._env_float("STAGED_POLL_SEC", 5.0),
        position_mode=getattr(grid_args, "position_mode", "auto"),
        recv_window=getattr(grid_args, "recv_window", 15000),
        env_file=getattr(grid_args, "env_file", None),
        once=False,
        audit=False,
    )


def run_once(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    import orderbook_staged_exit as staged

    staged_args = _staged_args(args)
    prefer = None
    if staged_args.direction == "long":
        prefer = True
    elif staged_args.direction == "short":
        prefer = False
    staged.manage_staged_once(
        symbol, staged_args, hedge, api, sec, filt, prefer_is_long=prefer,
    )


def sync_flat(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    """When flat: clear staged state via manage_staged_once (no-op if already idle)."""
    import orderbook_staged_exit as staged

    staged_args = _staged_args(args)
    prefer = None
    if staged_args.direction == "long":
        prefer = True
    elif staged_args.direction == "short":
        prefer = False
    staged.manage_staged_once(
        symbol, staged_args, hedge, api, sec, filt, prefer_is_long=prefer,
    )


def staged_blocks_grid_rearm(symbol: str) -> bool:
    """True while staged runner is active (no DCA / no new grid until flat + closed)."""
    import orderbook_staged_exit as staged

    phase = staged.load_state(symbol.upper()).get("phase", staged.PHASE_IDLE)
    return phase in (staged.PHASE_PARTIAL, staged.PHASE_TRAIL)


def dca_rearm_allowed(symbol: str) -> bool:
    """False after TP1 — DCA was cancelled on purpose; runner is SL/trail only."""
    return not staged_blocks_grid_rearm(symbol)


def staged_phase(symbol: str) -> str:
    import orderbook_staged_exit as staged

    return str(staged.load_state(symbol.upper()).get("phase", staged.PHASE_IDLE))
