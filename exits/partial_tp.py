"""Partial TP (default 70% @ +0.3% gross) for the structure + BE stack.

Arms a reduce-only TAKE_PROFIT_MARKET on ``--tp-partial-pct`` of the position
at entry ± ``--tp1-profit-pct`` (default **0.3%**, gross — fees not added).

Only arms when position notional (qty × entry) ≥ ``--partial-tp-min-notional``
(default **500** USDT). Smaller positions skip this layer.

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


def partial_tp_min_notional(args: argparse.Namespace) -> float:
    """Only arm partial TP when |qty×entry| ≥ this USDT (default 500)."""
    v = getattr(args, "partial_tp_min_notional", None)
    if v is not None:
        return float(v)
    return _env_float("PARTIAL_TP_MIN_NOTIONAL", 500.0)


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
    min_notional = partial_tp_min_notional(args)
    if partial <= 0 or partial >= 100 or profit_pct <= 0:
        return

    notional = abs(float(qty) * float(entry))
    if notional < min_notional:
        print(
            f"{grid.DIM}Partial TP skip · notional {notional:,.0f} USDT "
            f"< {min_notional:g} (need larger position){grid.RESET}"
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
        f"(+{profit_pct:g}% gross · notional {notional:,.0f} USDT ≥ {min_notional:g})"
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
        f"(+{profit_pct:g}% / {partial:g}% · ≥{min_notional:g} USDT){grid.RESET}"
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
