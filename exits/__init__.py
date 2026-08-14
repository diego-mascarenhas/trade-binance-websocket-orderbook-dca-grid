"""Exit strategy plugins for orderbook_dca_grid.py --supervise.

Composition model:
  --exit <eql|trailing|ob|pullback|ratchet|…>   primary close method
  --protect-be / --no-protect-be   optional BE SL addon (not stacked with ratchet)
  --partial-tp                     5× / burst partial (structure + ratchet)
  --also-structure                 overlay EQL/EQH close on top of ratchet
  --post-be trail                  optional trail *after* BE (structure/be only)
  --risk-reduce / --no-risk-reduce optional SHORT full SL at ATH + RISK_ATH_SL_PCT

Ratchet owns the BE algo tag (entry-floor SL, then walls). Partial TP and
structure overlays may close earlier when they would be the better trade.
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
EXIT_OB = "ob"
EXIT_PULLBACK = "pullback"
EXIT_RATCHET = "ratchet"
EXIT_NONE = "none"

# Backward-compat alias kept in normalize map
EXIT_BE_OB = EXIT_OB

# Aliases accepted from CLI / mobile / env
_EXIT_ALIASES = {
    "eq": EXIT_STRUCTURE,
    "eqh": EXIT_STRUCTURE,
    "eql": EXIT_STRUCTURE,
    "eqh_eql": EXIT_STRUCTURE,
    "structure_tp": EXIT_STRUCTURE,
    "trail": EXIT_TRAILING,
    "protect": EXIT_BE,
    "breakeven": EXIT_BE,
    "be_protect": EXIT_BE,
    "ob-long": EXIT_OB,
    "ob_long": EXIT_OB,
    "oblong": EXIT_OB,
    "be-ob": EXIT_OB,  # legacy: use --exit ob --protect-be
    "be_ob": EXIT_OB,
    "beob": EXIT_OB,
    "pb": EXIT_PULLBACK,
    "pull": EXIT_PULLBACK,
    "giveback": EXIT_PULLBACK,
    "support-be": EXIT_RATCHET,
    "support_be": EXIT_RATCHET,
    "ratchet-be": EXIT_RATCHET,
    "ratchet_be": EXIT_RATCHET,
    "levels": EXIT_RATCHET,
}

_LABELS = {
    EXIT_TRAILING: "trailing TP @ OB wall (+ optional BE)",
    EXIT_STAGED: "staged (TP1 + SL@entry + trail)",
    EXIT_STRUCTURE: "structure TP (LONG→EQH · SHORT→EQL) + optional BE / post-BE trail",
    EXIT_BE: "BE protect only (+ optional post-BE trail)",
    EXIT_OB: "soft-close on OB flip (SHORT→OB Long) + optional BE",
    EXIT_PULLBACK: "soft-close on adverse pullback from favorable extreme (+ optional BE)",
    EXIT_RATCHET: "ratchet SL to previous support/resistance as walls break",
    EXIT_NONE: "none",
}

_VALID = {
    EXIT_TRAILING,
    EXIT_STAGED,
    EXIT_STRUCTURE,
    EXIT_BE,
    EXIT_OB,
    EXIT_PULLBACK,
    EXIT_RATCHET,
    EXIT_NONE,
}

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
    foreign_n = 0
    try:
        foreign_n = grid.cancel_foreign_sl(sym, side_is_long, api, sec, recv)
    except Exception:
        pass
    return {"staged": staged_n, "close_algos": close_n, "foreign": foreign_n}


def protect_be_enabled(args: argparse.Namespace) -> bool:
    """BE protect addon — orthogonal to --exit (off with --no-protect-be)."""
    return bool(getattr(args, "protect_be", True))


def also_structure_enabled(args: argparse.Namespace) -> bool:
    """EQL/EQH overlay on top of --exit ratchet (off unless asked)."""
    v = getattr(args, "also_structure", None)
    if v is not None:
        return bool(v)
    raw = (os.getenv("ALSO_STRUCTURE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _run_optional_partial_tp(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> tuple[bool, float, float] | None:
    recv = int(getattr(args, "recv_window", 15000) or 15000)
    from exits.partial_tp import partial_tp_enabled, run_once as partial_once

    if not partial_tp_enabled(args):
        return side_is_long, qty, entry
    partial_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
    return _refresh_side(symbol, side_is_long, hedge, api, sec, recv)


def _refresh_side(
    symbol: str,
    side_is_long: bool,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> tuple[bool, float, float] | None:
    from orderbook_dca_grid import _detect_open_side

    still_long, still_qty, still_entry = _detect_open_side(
        symbol, hedge, api, sec, recv, prefer_is_long=side_is_long,
    )
    if still_long is None or still_qty <= 0:
        return None
    return still_long, still_qty, still_entry


def _run_optional_be(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    allow_post_be_trail: bool,
) -> tuple[bool, float, float] | None:
    """Run BE if enabled; return refreshed side or None if flat."""
    recv = int(getattr(args, "recv_window", 15000) or 15000)
    if not protect_be_enabled(args):
        return side_is_long, qty, entry

    from exits.be import run_once as be_once

    be_args = args
    if not allow_post_be_trail:
        be_args = argparse.Namespace(**vars(args))
        be_args.post_be = "none"
    be_once(symbol, side_is_long, qty, entry, be_args, hedge, api, sec, filt)
    return _refresh_side(symbol, side_is_long, hedge, api, sec, recv)


def _run_optional_risk_reduce(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> tuple[bool, float, float] | None:
    """Partial cut + far full SL above impulse high (SHORT). Refresh side after."""
    recv = int(getattr(args, "recv_window", 15000) or 15000)
    try:
        from exits.risk_reduce import enabled, run_once as risk_once

        if enabled(args):
            risk_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
    except Exception as exc:  # noqa: BLE001
        print(f"ATH SL skip: {exc}")
    return _refresh_side(symbol, side_is_long, hedge, api, sec, recv)


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

    # Orthogonal: impulse-high risk cut (SHORT) — before primary exit / BE
    refreshed = _run_optional_risk_reduce(
        symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
    )
    if refreshed is None:
        return
    side_is_long, qty, entry = refreshed

    if mode == EXIT_STAGED:
        from exits.staged import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return
    if mode == EXIT_BE:
        from exits.be import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return

    recv = int(getattr(args, "recv_window", 15000) or 15000)

    if mode == EXIT_TRAILING:
        refreshed = _run_optional_be(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
            allow_post_be_trail=False,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed
        from exits.trailing import run_once
        run_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return

    if mode == EXIT_OB:
        refreshed = _run_optional_be(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
            allow_post_be_trail=False,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed
        from exits.ob_long import run_once as ob_once
        ob_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return

    if mode == EXIT_PULLBACK:
        refreshed = _run_optional_be(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
            allow_post_be_trail=False,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed
        from exits.pullback import run_once as pb_once
        pb_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return

    if mode == EXIT_RATCHET:
        # Floor: ratchet SL (owns BE tag). Overlays may take a better exit first.
        recv = int(getattr(args, "recv_window", 15000) or 15000)
        refreshed = _run_optional_partial_tp(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed
        if also_structure_enabled(args):
            from exits.structure import run_once as structure_once

            structure_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
            refreshed = _refresh_side(symbol, side_is_long, hedge, api, sec, recv)
            if refreshed is None:
                return
            side_is_long, qty, entry = refreshed
        from exits.ratchet import run_once as ratchet_once
        ratchet_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
        return

    if mode == EXIT_STRUCTURE:
        # optional partial → optional BE (+ optional post-BE trail) → EQL/EQH
        refreshed = _run_optional_partial_tp(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed

        refreshed = _run_optional_be(
            symbol, side_is_long, qty, entry, args, hedge, api, sec, filt,
            allow_post_be_trail=True,
        )
        if refreshed is None:
            return
        side_is_long, qty, entry = refreshed

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
    try:
        from exits.risk_reduce import sync_flat as risk_flat

        risk_flat(symbol, args, hedge, api, sec, filt)
    except Exception:
        pass

    if mode == EXIT_STAGED:
        from exits.staged import sync_flat
        sync_flat(symbol, args, hedge, api, sec, filt)
        return
    if mode in (
        EXIT_BE, EXIT_OB, EXIT_STRUCTURE, EXIT_TRAILING, EXIT_PULLBACK, EXIT_RATCHET,
    ):
        # These modes may have armed BE via --protect-be (or ratchet owns BE).
        from exits.be import sync_flat
        sync_flat(symbol, args, hedge, api, sec, filt)
        if mode not in (EXIT_STRUCTURE, EXIT_PULLBACK, EXIT_RATCHET):
            return
        # also wipe leftover staged tags / pullback extreme / ratchet state
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
                "pullback_extreme": None,
                "ratchet_sl": None,
                "ratchet_seen_levels": [],
                "ratchet_broken": [],
                "ratchet_extreme": None,
            },
        )
        if n:
            print(f"Cleared {n} leftover staged exit algo(s) while flat ({mode}).")
    except Exception:
        pass
