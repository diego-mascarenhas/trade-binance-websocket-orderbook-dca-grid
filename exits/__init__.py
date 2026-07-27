"""Exit strategy plugins for orderbook_dca_grid.py --supervise.

Add new strategies here; the main bot only dispatches via run_exit_once().
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

EXIT_TRAILING = "trailing"
EXIT_STAGED = "staged"
EXIT_STRUCTURE = "structure"
EXIT_NONE = "none"

# Aliases accepted from CLI / mobile / env
_EXIT_ALIASES = {
    "eq": EXIT_STRUCTURE,
    "eqh": EXIT_STRUCTURE,
    "eql": EXIT_STRUCTURE,
    "eqh_eql": EXIT_STRUCTURE,
    "structure_tp": EXIT_STRUCTURE,
}

_LABELS = {
    EXIT_TRAILING: "trailing TP @ OB wall",
    EXIT_STAGED: "staged (TP1 + SL@entry + trail)",
    EXIT_STRUCTURE: "structure TP (LONG→EQH · SHORT→EQL)",
    EXIT_NONE: "none",
}

_VALID = {EXIT_TRAILING, EXIT_STAGED, EXIT_STRUCTURE, EXIT_NONE}


def normalize_exit_mode(raw: str | None) -> str | None:
    """Map aliases to canonical exit mode; None if empty/unknown."""
    if raw is None:
        return None
    mode = str(raw).strip().lower()
    if not mode:
        return None
    mode = _EXIT_ALIASES.get(mode, mode)
    return mode if mode in _VALID else None


def resolve_exit_mode(args: argparse.Namespace) -> str:
    """Effective exit mode from --exit, EXIT_MODE env, and legacy --no-tp."""
    mode = getattr(args, "exit_mode", None)
    if mode is not None:
        normalized = normalize_exit_mode(str(mode))
        if normalized is not None:
            return normalized
    if getattr(args, "no_tp", False):
        return EXIT_NONE
    env_mode = normalize_exit_mode(os.getenv("EXIT_MODE", ""))
    if env_mode is not None:
        return env_mode
    return EXIT_STAGED


def exit_mode_label(mode: str) -> str:
    return _LABELS.get(mode, mode)


def run_exit_once(
    mode: str,
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
    if mode == EXIT_NONE:
        return
    if mode == EXIT_TRAILING:
        from exits.trailing import run_once
    elif mode == EXIT_STAGED:
        from exits.staged import run_once
    elif mode == EXIT_STRUCTURE:
        from exits.structure import run_once
    else:
        raise ValueError(f"Unknown exit mode: {mode}")
    run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)


def run_exit_when_flat(
    mode: str,
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    """Clear staged state (and stray algos) when flat — supervise calls this each poll."""
    if mode != EXIT_STAGED:
        return
    from exits.staged import sync_flat
    sync_flat(symbol, args, hedge, api, sec, filt)
