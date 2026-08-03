"""Exit: ratchet stop to the previous support/resistance as levels break.

SHORT — as bid walls (supports) below entry are broken, move the reduce-only
STOP up to the *previous* (higher) support. LONG — mirror on ask walls.

Primary exit via ``--exit ratchet``. The stop itself is the exit; classic
``--protect-be`` is not stacked (this mode owns the BE algo tag).

Initial floor (before any break): entry ± ``--be-profit-pct`` once
``--ratchet-min-profit-pct`` is reached (same idea as BE protect arm).
"""

from __future__ import annotations

import argparse
import os
import statistics
from decimal import Decimal, ROUND_DOWN

from ob_signals import profit_pct

_CLOSE_REASONS: dict[str, str] = {}


def pop_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.pop(symbol.upper(), None)


def peek_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.get(symbol.upper())


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def break_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "ratchet_break_pct", None)
    if v is not None:
        return float(v)
    return _env_float("RATCHET_BREAK_PCT", 0.15)


def min_profit_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "ratchet_min_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("RATCHET_MIN_PROFIT_PCT", 0.3)


def wall_min_mult(args: argparse.Namespace) -> float:
    v = getattr(args, "ratchet_wall_min_mult", None)
    if v is not None:
        return float(v)
    return _env_float("RATCHET_WALL_MIN_MULT", 1.0)


def be_lock_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "be_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("BE_PROFIT_PCT", 0.3)


def _levels_from_book(
    bids: list[list[float]],
    asks: list[list[float]],
    *,
    is_long: bool,
    entry: float,
    min_mult: float,
) -> list[float]:
    """Support (SHORT) or resistance (LONG) wall prices beyond entry."""
    side_lvls = asks if is_long else bids
    qty_all = [float(q) for _, q in (bids + asks) if float(q) > 0]
    med = statistics.median(qty_all) if qty_all else 0.0
    min_wall = med * min_mult if med > 0 else 0.0

    out: list[float] = []
    for p_raw, q_raw in side_lvls:
        p, q = float(p_raw), float(q_raw)
        if p <= 0 or q < min_wall:
            continue
        if is_long and p <= entry:
            continue
        if not is_long and p >= entry:
            continue
        out.append(p)
    # SHORT: high→low (nearest support first). LONG: low→high.
    out.sort(reverse=not is_long)
    # Dedupe near-identical prices
    deduped: list[float] = []
    for p in out:
        if not deduped or abs(p - deduped[-1]) / entry * 100 >= 0.05:
            deduped.append(p)
    return deduped


def _broken_levels(
    levels: list[float],
    mark: float,
    *,
    is_long: bool,
    pierce_pct: float,
) -> list[float]:
    """Levels the mark has pierced by pierce_pct."""
    broken: list[float] = []
    for lvl in levels:
        if is_long:
            # Break resistance: mark above lvl
            if mark >= lvl * (1 + pierce_pct / 100):
                broken.append(lvl)
        else:
            # Break support: mark below lvl
            if mark <= lvl * (1 - pierce_pct / 100):
                broken.append(lvl)
    return broken


def _previous_level(
    levels: list[float],
    broken: list[float],
    *,
    is_long: bool,
    entry: float,
    lock_pct: float,
) -> tuple[float, str]:
    """SL at the level before the deepest break; else entry±lock."""
    if is_long:
        entry_floor = entry * (1 - lock_pct / 100) if lock_pct > 0 else entry
    else:
        entry_floor = entry * (1 + lock_pct / 100) if lock_pct > 0 else entry

    if not broken:
        label = f"entry+{lock_pct:g}%" if lock_pct > 0 else "entry"
        return entry_floor, label

    deepest = max(broken) if is_long else min(broken)
    if is_long:
        # Previous = next lower resistance (closer to entry), else entry floor
        lower = [l for l in levels if l < deepest]
        if lower:
            prev = max(lower)
            return prev, f"prev resist {prev:g}"
        return entry_floor, f"entry+{lock_pct:g}% (first break)"
    # SHORT: previous = next higher support
    higher = [l for l in levels if l > deepest]
    if higher:
        prev = min(higher)
        return prev, f"prev support {prev:g}"
    return entry_floor, f"entry+{lock_pct:g}% (first break)"


def _sl_is_improvement(
    is_long: bool,
    new_sl: float,
    old_sl: float | None,
) -> bool:
    if old_sl is None or old_sl <= 0:
        return True
    # LONG: higher SL locks more. SHORT: lower SL locks more.
    if is_long:
        return new_sl > old_sl
    return new_sl < old_sl


def _place_sl(
    symbol: str,
    is_long: bool,
    qty: float,
    trigger: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    dry_run: bool,
    label: str,
) -> float | None:
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return None
    # Round stop away from market so it does not cross immediately
    if is_long:
        trig_d = grid._round_to(trigger, tick, ROUND_DOWN)
    else:
        from decimal import ROUND_UP
        trig_d = grid._round_to(trigger, tick, ROUND_UP)
    trig = float(trig_d)
    qty_str = f"{qty_d:.{qty_dp}f}"
    trig_str = f"{trig_d:.{price_dp}f}"
    close_side = "SELL" if is_long else "BUY"

    staged.cancel_our_algos(symbol, "BE", api, sec, recv)

    if dry_run:
        print(
            f"{grid.DIM}DRY-RUN ratchet SL {close_side} {qty_str} @ {trig_str} "
            f"({label}){grid.RESET}"
        )
        return trig

    mark = staged.get_mark_price(symbol, api, sec, recv)
    if staged._stop_would_immediately_trigger(is_long, trig, mark, tick):
        print(
            f"{grid.YELLOW}Ratchet SL ({label}) would trigger now "
            f"(mark {grid.price_fmt(mark)}) — closing MARKET{grid.RESET}"
        )
        staged._market_reduce_qty(
            symbol, is_long, qty_d, hedge, filt, api, sec, recv,
        )
        _CLOSE_REASONS[symbol.upper()] = f"Ratchet · {label} (immediate)"
        return None

    try:
        resp = staged.place_algo_order(
            symbol, is_long, "STOP_MARKET", qty_str, trig_str,
            hedge, api, sec, recv, client_tag="BE",
        )
    except RuntimeError as exc:
        if not staged._is_immediate_trigger_error(exc):
            raise
        print(f"{grid.YELLOW}Ratchet SL rejected (-2021) — closing MARKET{grid.RESET}")
        staged._market_reduce_qty(
            symbol, is_long, qty_d, hedge, filt, api, sec, recv,
        )
        _CLOSE_REASONS[symbol.upper()] = f"Ratchet · {label} (immediate)"
        return None

    print(
        f"{grid.BOLD}{grid.GREEN}✓ Ratchet SL {close_side} {qty_str} @ {trig_str} "
        f"({label}) algoId={resp.get('algoId')}{grid.RESET}"
    )
    return trig


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
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    if entry <= 0 or qty <= 0:
        return

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    side = "LONG" if side_is_long else "SHORT"
    pierce = break_pct(args)
    min_pct = min_profit_pct(args)
    lock_pct = be_lock_pct(args)
    min_mult = wall_min_mult(args)

    try:
        mark = staged.get_mark_price(symbol, api, sec, recv)
        depth = grid.fetch_depth(symbol, getattr(args, "limit", 50))
        bids = [[float(p), float(q)] for p, q in (depth.get("bids") or [])]
        asks = [[float(p), float(q)] for p, q in (depth.get("asks") or [])]
    except Exception as exc:
        print(f"{grid.YELLOW}Ratchet depth skip: {exc}{grid.RESET}")
        return
    if mark <= 0:
        return

    pnl = profit_pct(entry, mark, side_is_long)
    state = staged.load_state(symbol.upper())
    old_sl = state.get("ratchet_sl")
    try:
        old_sl_f = float(old_sl) if old_sl is not None else None
    except (TypeError, ValueError):
        old_sl_f = None

    levels = _levels_from_book(
        bids, asks, is_long=side_is_long, entry=entry, min_mult=min_mult,
    )
    broken = _broken_levels(levels, mark, is_long=side_is_long, pierce_pct=pierce)

    if pnl < min_pct and old_sl_f is None:
        print(
            f"{grid.DIM}Ratchet wait · {side} pnl={pnl:+.3f}% "
            f"(need ≥{min_pct:g}% to arm entry floor) · "
            f"{len(levels)} wall(s){grid.RESET}"
        )
        return

    target, label = _previous_level(
        levels, broken, is_long=side_is_long, entry=entry, lock_pct=lock_pct,
    )

    # Never worsen an existing ratchet SL
    if old_sl_f is not None and not _sl_is_improvement(side_is_long, target, old_sl_f):
        target = old_sl_f
        label = f"hold {grid.price_fmt(old_sl_f)}"

    existing = staged.find_our_algo(symbol, "BE", api, sec, recv)
    need_place = existing is None or (
        old_sl_f is None or abs(target - old_sl_f) / entry * 100 >= 0.02
    )

    state.update({
        "phase": "ratchet",
        "symbol": symbol.upper(),
        "is_long": side_is_long,
        "entry": float(entry),
        "entry_anchor": float(entry),
        "be_protect_armed": True,
        "ratchet_levels": levels[:12],
        "ratchet_broken": broken[:12],
    })

    if not need_place:
        print(
            f"{grid.DIM}Ratchet sync · {side} pnl={pnl:+.3f}% · "
            f"SL@{grid.price_fmt(old_sl_f or target)} · "
            f"broken={len(broken)}/{len(levels)} · {label}{grid.RESET}"
        )
        staged.save_state(symbol.upper(), state)
        return

    if not _sl_is_improvement(side_is_long, target, old_sl_f) and existing is not None:
        print(
            f"{grid.DIM}Ratchet hold · {side} SL@{grid.price_fmt(old_sl_f)} "
            f"· broken={len(broken)}/{len(levels)}{grid.RESET}"
        )
        staged.save_state(symbol.upper(), state)
        return

    placed = _place_sl(
        symbol, side_is_long, qty, target, filt, hedge, api, sec, recv,
        dry_run=bool(getattr(args, "dry_run", False)),
        label=label,
    )
    if placed is not None:
        state["ratchet_sl"] = placed
        state["be_price"] = placed
        if old_sl_f is None or _sl_is_improvement(side_is_long, placed, old_sl_f):
            try:
                import telegram_notify as telegram

                lev = grid.get_symbol_leverage(symbol, api, sec, recv)
                _, upnl = staged.position_pnl(
                    symbol, side_is_long, hedge, api, sec, recv,
                )
                telegram.notify_profit_lock_sl(
                    symbol.upper(), side, qty, entry, placed,
                    closed_pct=0.0,
                    runner_pct=100.0,
                    trigger=f"ratchet · {label}",
                    closed_qty=0.0,
                    leverage=lev,
                    pnl_usdt=upnl,
                    hashtag="#BE",
                )
            except Exception:
                pass
    staged.save_state(symbol.upper(), state)
