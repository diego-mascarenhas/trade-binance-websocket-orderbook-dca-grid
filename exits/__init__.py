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
EXIT_BE = "be"
EXIT_NONE = "none"

# Aliases accepted from CLI / mobile / env
_EXIT_ALIASES = {
    "eq": EXIT_STRUCTURE,
    "eqh": EXIT_STRUCTURE,
    "eql": EXIT_STRUCTURE,
    "eqh_eql": EXIT_STRUCTURE,
    "structure_tp": EXIT_STRUCTURE,
    "protect": EXIT_BE,
    "breakeven": EXIT_BE,
    "be_protect": EXIT_BE,
}

_LABELS = {
    EXIT_TRAILING: "trailing TP @ OB wall",
    EXIT_STAGED: "staged (TP1 + SL@entry + trail)",
    EXIT_STRUCTURE: "structure TP (LONG→EQH · SHORT→EQL) + optional BE protect",
    EXIT_BE: "BE protect only (no TP)",
    EXIT_NONE: "none",
}

_VALID = {EXIT_TRAILING, EXIT_STAGED, EXIT_STRUCTURE, EXIT_BE, EXIT_NONE}


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


def clear_exit_presets(
    symbol: str,
    side_is_long: bool,
    api: str,
    sec: str,
    recv: int,
) -> dict[str, int]:
    """Remove previous exit preset (staged algos/state + close-side TP/SL/trail).

    Call before arming a new exit mode so old conditionals do not linger.
    Does not cancel DCA grid LIMIT orders or the position itself.
    """
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged
    from exits.structure import cancel_close_algos

    sym = symbol.upper()
    staged_n = staged.cancel_all_staged_algos(sym, api, sec, recv)
    try:
        staged.save_state(
            sym,
            {
                "phase": staged.PHASE_IDLE,
                "symbol": sym,
                "remain_qty": 0.0,
                "algo_ids": {},
            },
        )
    except Exception:
        pass
    close_n = cancel_close_algos(sym, side_is_long, api, sec, recv)
    # Also drop foreign reduce-side TP/SL that staged cancel skipped / trailing left.
    foreign_n = 0
    try:
        foreign_n = grid.cancel_foreign_sl(sym, side_is_long, api, sec, recv)
    except Exception:
        pass
    return {"staged": staged_n, "close_algos": close_n, "foreign": foreign_n}


def protect_be_enabled(args: argparse.Namespace) -> bool:
    """BE protect addon (default on for --exit structure; off with --no-protect-be)."""
    return bool(getattr(args, "protect_be", True))


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
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return
    if mode == EXIT_STAGED:
        from exits.staged import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return
    if mode == EXIT_BE:
        from exits.be import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return
    if mode == EXIT_STRUCTURE:
        # Protect first (exchange SL), then structure TP (EQL/EQH soft close).
        if protect_be_enabled(args):
            from exits.be import run_once as be_once
            be_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
            # Position may have been closed by immediate BE trigger
            from orderbook_dca_grid import _detect_open_side
            recv = int(getattr(args, "recv_window", 15000) or 15000)
            still_long, still_qty, still_entry = _detect_open_side(
                symbol, hedge, api, sec, recv, prefer_is_long=side_is_long,
            )
            if still_long is None or still_qty <= 0:
                return
            side_is_long, qty, entry = still_long, still_qty, still_entry
        from exits.structure import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return
    raise ValueError(f"Unknown exit mode: {mode}")


def run_exit_when_flat(
    mode: str,
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    """Clear exit leftovers when flat.

    Staged/BE: full sync_flat. Other modes: still drop stray staged algos/state.
    """
    if mode == EXIT_STAGED:
        from exits.staged import sync_flat
        sync_flat(symbol, args, hedge, api, sec, filt)
        return
    if mode in (EXIT_BE, EXIT_STRUCTURE):
        # Structure may have armed BE protect — clear on flat either way.
        from exits.be import sync_flat
        sync_flat(symbol, args, hedge, api, sec, filt)
        if mode == EXIT_BE:
            return
        # structure: also wipe any leftover staged tags beyond BE
    # Left staged mode (or never used it) — wipe idle staged artifacts.
    try:
        import orderbook_staged_exit as staged

        recv = int(getattr(args, "recv_window", 15000) or 15000)
        n = staged.cancel_all_staged_algos(symbol, api, sec, recv)
        staged.save_state(
            symbol.upper(),
            {
                "phase": staged.PHASE_IDLE,
                "symbol": symbol.upper(),
                "remain_qty": 0.0,
                "algo_ids": {},
                "be_protect_armed": False,
            },
        )
        if n:
            print(f"Cleared {n} leftover staged exit algo(s) while flat ({mode}).")
    except Exception:
        pass
