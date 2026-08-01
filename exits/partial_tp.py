"""Partial TP (default 70% @ +0.3% gross) for the structure + BE stack.

Arms a reduce-only TAKE_PROFIT_MARKET on ``--tp-partial-pct`` of the position
at entry ± ``--tp1-profit-pct`` (default **0.3%**, gross — fees not added).

Only arms when position notional (qty × avg entry) reaches a size gate:
  • default: ``--partial-tp-min-entry-pct`` of the entry base size
    (default **500%** = 5× entry — typically mid-grid / ~5th DCA fill)
  • optional absolute override: ``--partial-tp-min-notional`` /
    ``PARTIAL_TP_MIN_NOTIONAL``

Does **not** cancel BE / post-BE trail algos. On TP1 fill: cancel leftover DCA
limits so the runner is not re-averaged; BE/structure continue on the remainder.
"""

from __future__ import annotations

import argparse
import os
from decimal import Decimal, ROUND_DOWN


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_float_optional(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def partial_tp_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "partial_tp", True))


def tp_partial_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "tp_partial_pct", None)
    if v is not None:
        return float(v)
    return _env_float("TP_PARTIAL_PCT", 70.0)


def tp1_profit_pct(args: argparse.Namespace) -> float:
    """Gross TP distance from entry (fees not added). Default 0.3%."""
    v = getattr(args, "tp1_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("TP1_PROFIT_PCT", 0.3)


def partial_tp_min_entry_pct(args: argparse.Namespace) -> float:
    """Position must reach this %% of entry base size (default 500 = 5×)."""
    v = getattr(args, "partial_tp_min_entry_pct", None)
    if v is not None:
        return float(v)
    return _env_float("PARTIAL_TP_MIN_ENTRY_PCT", 500.0)


def resolve_entry_base_usdt(
    args: argparse.Namespace,
    api: str,
    sec: str,
    recv: int,
) -> float:
    """Entry base size in USDT (BASE_SIZE or WALLET_PCT × wallet)."""
    base = float(getattr(args, "base_size", 0) or 0)
    if base > 0:
        return base
    try:
        import orderbook_dca_grid as grid

        bal = grid.get_wallet_balance(api, sec, recv)
        pct = float(getattr(args, "wallet_pct", None) or _env_float("WALLET_PCT", 10.0))
        if bal > 0 and pct > 0:
            return bal * pct / 100.0
    except Exception:
        pass
    return 0.0


def partial_tp_threshold(
    args: argparse.Namespace,
    *,
    api: str,
    sec: str,
    recv: int,
) -> tuple[float, str]:
    """Return (min_notional_usdt, human label) for the arm gate.

    Absolute ``--partial-tp-min-notional`` / ``PARTIAL_TP_MIN_NOTIONAL`` wins when set.
    Otherwise: entry_base × ``partial_tp_min_entry_pct`` / 100.
    """
    abs_v = getattr(args, "partial_tp_min_notional", None)
    if abs_v is None:
        abs_v = _env_float_optional("PARTIAL_TP_MIN_NOTIONAL")
    if abs_v is not None:
        thr = float(abs_v)
        return thr, f"{thr:g} USDT (absolute)"

    pct = partial_tp_min_entry_pct(args)
    base = resolve_entry_base_usdt(args, api, sec, recv)
    if base <= 0 or pct <= 0:
        # No reliable entry size — refuse to arm rather than use a stale fixed floor
        return float("inf"), "unresolved entry base"

    thr = base * (pct / 100.0)
    return thr, f"{pct:g}% of entry {base:,.2f} USDT (= {thr:,.2f})"


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
    if not partial_tp_enabled(args):
        return
    if entry <= 0 or qty <= 0:
        return

    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    dry = bool(getattr(args, "dry_run", False))
    partial = tp_partial_pct(args)
    profit_pct = tp1_profit_pct(args)
    min_notional, thr_label = partial_tp_threshold(
        args, api=api, sec=sec, recv=recv,
    )
    if partial <= 0 or partial >= 100 or profit_pct <= 0:
        return

    notional = abs(float(qty) * float(entry))
    if notional < min_notional:
        print(
            f"{grid.DIM}Partial TP skip · notional {notional:,.0f} USDT "
            f"< {thr_label}{grid.RESET}"
        )
        return

    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    side = "LONG" if side_is_long else "SHORT"
    sym = symbol.upper()

    state = staged.load_state(sym)
    # Detect TP1 fill: position shrank vs what we armed
    armed = float(state.get("partial_tp_armed_qty", 0) or 0)
    tp1_qty = float(state.get("partial_tp_qty", 0) or 0)
    tol = float(step) * 1.5
    if (
        bool(state.get("partial_tp_armed"))
        and armed > 0
        and tp1_qty > 0
        and qty < armed - tol
        and qty <= (armed - tp1_qty) + tol
    ):
        print(
            f"{grid.GREEN}✓ Partial TP filled · {side} ~{partial:g}% closed "
            f"→ runner {qty:g} (cancel DCA, keep BE/structure){grid.RESET}"
        )
        try:
            grid.cancel_dca_grid_orders(sym, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel DCA after partial TP: {exc}{grid.RESET}")
        try:
            import telegram_notify as telegram

            lev = grid.get_symbol_leverage(sym, api, sec, recv)
            _, upnl = staged.position_pnl(sym, side_is_long, hedge, api, sec, recv)
            telegram.notify_tp1_filled(
                sym, side, tp1_qty, qty, entry,
                tp1_price=float(state.get("partial_tp_price", entry) or entry),
                leverage=lev, pnl_usdt=upnl,
            )
        except Exception:
            pass
        state["partial_tp_armed"] = False
        state["partial_tp_filled"] = True
        staged.cancel_our_algos(sym, "TP1", api, sec, recv)
        algo_ids = dict(state.get("algo_ids") or {})
        algo_ids.pop("tp1", None)
        state["algo_ids"] = algo_ids
        staged.save_state(sym, state)
        return

    if bool(state.get("partial_tp_filled")):
        # Already took the 70% — do not re-arm on the runner
        return

    existing = staged.find_our_algo(sym, "TP1", api, sec, recv)
    tp1_trig_f = staged.profit_target_price(entry, side_is_long, profit_pct, tick)
    tp1_d, remain_d = staged.split_partial_qty(
        qty, partial, step, filt["min_qty"], filt["min_notional"], tp1_trig_f,
    )
    if tp1_d <= 0 or remain_d <= 0:
        print(f"{grid.YELLOW}Partial TP skip · qty too small to split {partial:g}%{grid.RESET}")
        return

    tp1_str = f"{tp1_d:.{qty_dp}f}"
    tp1_trig = f"{tp1_trig_f:.{price_dp}f}"
    close_side = "SELL" if side_is_long else "BUY"

    if existing and staged._algo_qty_matches(existing, tp1_str, step):
        try:
            trigger = float(existing.get("triggerPrice", 0) or 0)
        except (TypeError, ValueError):
            trigger = 0.0
        if abs(trigger - tp1_trig_f) <= float(tick):
            print(
                f"{grid.DIM}Partial TP armed · {side} {partial:g}% "
                f"notional {notional:,.0f} USDT · "
                f"TAKE_PROFIT @ {tp1_trig} (+{profit_pct:g}% gross){grid.RESET}"
            )
            return

    # Replace only our TP1 — never wipe BE / trail
    staged.cancel_our_algos(sym, "TP1", api, sec, recv)

    mark = staged.get_mark_price(sym, api, sec, recv)
    if staged.profit_target_hit(entry, mark, side_is_long, profit_pct):
        print(
            f"{grid.YELLOW}Partial TP already hit (mark) — "
            f"{close_side} MARKET {tp1_str} ({partial:g}%){grid.RESET}"
        )
        if not dry:
            staged._market_reduce_qty(
                sym, side_is_long, tp1_d, hedge, filt, api, sec, recv,
            )
            try:
                grid.cancel_dca_grid_orders(sym, api, sec, recv)
            except Exception:
                pass
        state.update({
            "partial_tp_armed": False,
            "partial_tp_filled": True,
            "partial_tp_qty": float(tp1_d),
            "partial_tp_price": tp1_trig_f,
            "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
        })
        staged.save_state(sym, state)
        return

    print(
        f"{close_side} TAKE_PROFIT_MARKET {tp1_str} ({partial:g}%) @ {tp1_trig} "
        f"(+{profit_pct:g}% gross · notional {notional:,.0f} USDT ≥ {thr_label})"
    )
    if dry:
        return

    try:
        resp = staged.place_algo_order(
            sym, side_is_long, "TAKE_PROFIT_MARKET", tp1_str, tp1_trig,
            hedge, api, sec, recv, client_tag="TP1",
        )
    except RuntimeError as exc:
        if staged._is_immediate_trigger_error(exc):
            print(f"{grid.YELLOW}Partial TP -2021 — market partial {tp1_str}{grid.RESET}")
            staged._market_reduce_qty(
                sym, side_is_long, tp1_d, hedge, filt, api, sec, recv,
            )
            try:
                grid.cancel_dca_grid_orders(sym, api, sec, recv)
            except Exception:
                pass
            state.update({
                "partial_tp_armed": False,
                "partial_tp_filled": True,
                "partial_tp_qty": float(tp1_d),
                "partial_tp_price": tp1_trig_f,
                "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
            })
            staged.save_state(sym, state)
            return
        raise

    print(
        f"{grid.GREEN}✓ Partial TP algoId={resp.get('algoId')} "
        f"(+{profit_pct:g}% / {partial:g}% · ≥ {thr_label}){grid.RESET}"
    )
    algo_ids = dict(state.get("algo_ids") or {})
    algo_ids["tp1"] = resp.get("algoId")
    state.update({
        "phase": state.get("phase") or "partial_tp",
        "symbol": sym,
        "is_long": side_is_long,
        "entry_anchor": float(entry),
        "entry": float(entry),
        "partial_tp_armed": True,
        "partial_tp_filled": False,
        "partial_tp_qty": float(tp1_d),
        "partial_tp_remain": float(remain_d),
        "partial_tp_price": tp1_trig_f,
        "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
        "partial_tp_profit_pct": profit_pct,
        "algo_ids": algo_ids,
    })
    staged.save_state(sym, state)
    try:
        import telegram_notify as telegram

        lev = grid.get_symbol_leverage(sym, api, sec, recv)
        telegram.notify_staged_armed(
            sym, side, float(qty), entry, tp1_trig_f, partial,
            tp1_qty=float(tp1_d), leverage=lev,
        )
    except Exception:
        pass
