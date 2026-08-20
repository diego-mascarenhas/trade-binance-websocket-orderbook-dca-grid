"""Partial TP (default 70% @ +0.3% net + fees) for the structure + BE stack.

When position notional (qty × avg entry) reaches 5× the entry base
(``--partial-tp-min-entry-pct``, default 500):

  • arm TAKE_PROFIT_MARKET on ``--tp-partial-pct`` (default **70%**)
  • trigger = entry ± (``--tp1-profit-pct`` + ``--tp-fee-buffer``)
    (default **0.3% net + 0.12% fees**). Binance reduces as soon as mark
    touches that price.
    • freeze auto-DCA at ``--dca-max-entry-pct`` when set (default **0** = off):
    cancel leftover safety orders and do not re-arm more adds.
    Account Margin Ratio (``MARGIN_RATIO_SOFT`` / ``HARD``, default **5%**)
    is the live size brake. A new ★ may still place one more grid past a
    custom per-symbol cap.

A favorable burst (``--partial-tp-burst-pct``, default 2%) can skip the 5×
TP gate so a 1× fill that explodes still gets the partial. The DCA cap
still applies (burst does not authorize more size).

On TP1 fill: cancel leftover TP1 + LIMIT DCA. Keep the ATH catastrophe
SL (``RF``) and keep the chosen ``--exit`` (ratchet / trail / structure /
BE) on the runner. Do not place a new grid here — the supervisor re-arms
a fresh DCA-only grid on the next poll (position open + 0 LIMITs).
Do not arm a second 70% TP on this position.
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
    """Net TP distance from entry (fees added separately). Default 0.3%."""
    v = getattr(args, "tp1_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("TP1_PROFIT_PCT", 0.3)


def tp_fee_buffer_pct(args: argparse.Namespace) -> float:
    """Round-trip fee+slippage %% added on top of the 0.3% net TP. Default 0.12."""
    v = getattr(args, "tp_fee_buffer", None)
    if v is not None:
        return float(v)
    return _env_float("TP_FEE_BUFFER", 0.12)


def tp1_gross_pct(args: argparse.Namespace) -> float:
    """Trigger distance: net TP1 + fee buffer (stays green after round-trip)."""
    return tp1_profit_pct(args) + max(tp_fee_buffer_pct(args), 0.0)


def partial_tp_min_entry_pct(args: argparse.Namespace) -> float:
    """Position must reach this %% of entry base size (default 500 = 5×)."""
    v = getattr(args, "partial_tp_min_entry_pct", None)
    if v is not None:
        return float(v)
    return _env_float("PARTIAL_TP_MIN_ENTRY_PCT", 500.0)


def partial_tp_burst_pct(args: argparse.Namespace) -> float:
    """Favorable move %% that bypasses the 5× size gate (0 = off). Default 2."""
    v = getattr(args, "partial_tp_burst_pct", None)
    if v is not None:
        return float(v)
    return _env_float("PARTIAL_TP_BURST_PCT", 2.0)


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


def dca_max_entry_pct(args: argparse.Namespace) -> float:
    """Max filled notional as %% of entry base. 0 = unlimited.

    Default **0** — account Margin Ratio (5%) is the size brake, not a
    multiple of the tiny entry ticket. Set ``--dca-max-entry-pct`` /
    ``DCA_MAX_ENTRY_PCT`` if you still want a per-symbol multiple.
    """
    v = getattr(args, "dca_max_entry_pct", None)
    if v is not None:
        return float(v)
    env = _env_float_optional("DCA_MAX_ENTRY_PCT")
    if env is not None:
        return float(env)
    return 0.0


def dca_cap_threshold(
    args: argparse.Namespace,
    *,
    api: str,
    sec: str,
    recv: int,
) -> tuple[float, str]:
    """Return (max_notional_usdt, label). ``0`` / non-finite = no cap."""
    pct = dca_max_entry_pct(args)
    if pct <= 0:
        return 0.0, "off"
    base = resolve_entry_base_usdt(args, api, sec, recv)
    if base <= 0:
        return float("inf"), "unresolved entry base"
    thr = base * (pct / 100.0)
    return thr, f"{pct:g}% of entry {base:,.2f} USDT (= {thr:,.2f})"


def position_notional(qty: float, entry: float) -> float:
    return abs(float(qty or 0) * float(entry or 0))


def position_over_dca_cap(qty: float, entry: float, cap: float) -> bool:
    """True when qty×avg entry already meets/exceeds the optional size cap."""
    if cap <= 0 or cap == float("inf"):
        return False
    return position_notional(qty, entry) >= cap - 1e-9


def trim_orders_to_dca_cap(
    orders: list[dict],
    current_notional: float,
    cap: float,
) -> list[dict]:
    """Keep leading orders whose size still fits under ``cap``."""
    if cap <= 0 or cap == float("inf"):
        return list(orders)
    room = float(cap) - float(current_notional or 0)
    if room <= 0:
        return []
    kept: list[dict] = []
    used = 0.0
    for o in orders:
        sz = float(o.get("size_usdt") or 0)
        if sz <= 0:
            continue
        if used + sz > room + 0.01:
            break
        kept.append(o)
        used += sz
    return kept


def partial_tp_already_filled(symbol: str) -> bool:
    """True after the 70% TP on this position (no second partial until flat)."""
    try:
        import orderbook_staged_exit as staged

        return bool((staged.load_state(symbol.upper()) or {}).get("partial_tp_filled"))
    except Exception:
        return False


def sl_paused_after_partial(symbol: str) -> bool:
    """Legacy hook. Chosen ``--exit`` stays on after TP1; always False."""
    return False


def dca_adds_blocked(
    symbol: str,
    args: argparse.Namespace,
    qty: float,
    entry: float,
    *,
    api: str,
    sec: str,
    recv: int,
) -> tuple[bool, str]:
    """Whether to freeze DCA (cancel leftovers + skip re-arm).

    After partial TP leftover LIMITs are cancelled so the supervisor can
    place a fresh grid — only a custom size cap (and the ★ one-grid bypass) freeze adds.
    """
    try:
        import star_rearm as sr

        if sr.pending(symbol) or sr.grid_active(symbol):
            return False, ""
    except Exception:
        pass
    cap, label = dca_cap_threshold(args, api=api, sec=sec, recv=recv)
    if position_over_dca_cap(qty, entry, cap):
        notional = position_notional(qty, entry)
        return True, f"notional {notional:,.0f} USDT ≥ {label}"
    return False, ""


def _cancel_leftover_tp1(
    symbol: str,
    _side_is_long: bool,
    api: str,
    sec: str,
    recv: int,
) -> int:
    """Drop leftover TP1 / legacy RR after the 70% fill. Keep ``--exit`` + ATH ``RF``."""
    import orderbook_staged_exit as staged

    killed = 0
    for tag in ("TP1", "RR"):
        try:
            killed += staged.cancel_our_algos(symbol, tag, api, sec, recv)
        except Exception:
            pass
    return killed


def _on_partial_tp_fill(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    _filt: dict,
    state: dict,
    *,
    tp1_qty: float,
    partial_pct: float,
) -> None:
    """Harvest 70%, wipe stale LIMITs, keep ``--exit`` + ATH SL on the runner."""
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    dry = bool(getattr(args, "dry_run", False))
    side = "LONG" if side_is_long else "SHORT"
    print(
        f"{grid.GREEN}✓ Partial TP filled · {side} ~{partial_pct:g}% closed "
        f"→ runner {qty:g} (--exit kept · ATH SL kept · re-arm grid){grid.RESET}"
    )
    if dry:
        print(
            f"{grid.DIM}DRY-RUN · would cancel leftover TP1 and LIMITs; "
            f"keep --exit + ATH SL; supervisor re-arms a fresh grid{grid.RESET}"
        )
        killed = 0
    else:
        killed = _cancel_leftover_tp1(symbol, side_is_long, api, sec, recv)
        try:
            grid.cancel_dca_grid_orders(symbol, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel DCA after partial TP: {exc}{grid.RESET}")
    if killed:
        print(f"{grid.YELLOW}Cancelled {killed} leftover TP1/RR algo(s) after partial TP{grid.RESET}")

    algo_ids = dict(state.get("algo_ids") or {})
    algo_ids.pop("tp1", None)
    algo_ids.pop("rr", None)
    state.update({
        "partial_tp_armed": False,
        "partial_tp_filled": True,
        "sl_paused_after_partial": False,
        "algo_ids": algo_ids,
    })
    staged.save_state(symbol.upper(), state)

    try:
        import telegram_notify as telegram

        lev = grid.get_symbol_leverage(symbol, api, sec, recv)
        _, upnl = staged.position_pnl(symbol, side_is_long, hedge, api, sec, recv)
        telegram.notify_tp1_filled(
            symbol.upper(), side, tp1_qty, qty, entry,
            tp1_price=float(state.get("partial_tp_price", entry) or entry),
            leverage=lev, pnl_usdt=upnl,
            note="--exit kept · ATH SL kept · re-arming grid",
        )
    except Exception:
        pass


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
    net_pct = tp1_profit_pct(args)
    fee_pct = tp_fee_buffer_pct(args)
    profit_pct = tp1_gross_pct(args)
    try:
        from exits.recovery import recovery_pct

        rec = float(recovery_pct(symbol, qty=qty, entry=entry) or 0)
        if rec > 0:
            profit_pct = profit_pct + rec
    except Exception:
        rec = 0.0
    min_notional, thr_label = partial_tp_threshold(
        args, api=api, sec=sec, recv=recv,
    )
    if partial <= 0 or partial >= 100 or profit_pct <= 0:
        return

    # qty×entry = USDT-M notional. Do not multiply by leverage (Binance Size in
    # USDT is already qty×mark; 30 USDT @ 50x is still 30 notional, ~0.6 margin).
    notional = abs(float(qty) * float(entry))
    sym = symbol.upper()
    burst_pct = partial_tp_burst_pct(args)
    mark = staged.get_mark_price(sym, api, sec, recv)
    fav_pct = staged.profit_pct(entry, mark, side_is_long) if mark > 0 else 0.0
    burst = burst_pct > 0 and fav_pct >= burst_pct

    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    side = "LONG" if side_is_long else "SHORT"

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
        _on_partial_tp_fill(
            sym, side_is_long, qty, entry, args, hedge, api, sec, filt, state,
            tp1_qty=tp1_qty, partial_pct=partial,
        )
        return

    if bool(state.get("partial_tp_filled")):
        # Already took the 70% — do not arm a second partial; --exit owns the runner.
        if bool(state.get("sl_paused_after_partial")):
            state["sl_paused_after_partial"] = False
            staged.save_state(sym, state)
        return

    already_armed = bool(state.get("partial_tp_armed"))
    if notional < min_notional and not burst and not already_armed:
        print(
            f"{grid.DIM}Partial TP skip · notional {notional:,.0f} USDT "
            f"< {thr_label} · pnl {fav_pct:+.2f}% < burst {burst_pct:g}%{grid.RESET}"
        )
        return
    if burst and notional < min_notional and not already_armed:
        print(
            f"{grid.YELLOW}Partial TP burst · pnl {fav_pct:+.2f}% ≥ {burst_pct:g}% "
            f"· notional {notional:,.0f} USDT < {thr_label} → skip size gate{grid.RESET}"
        )

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
                f"TAKE_PROFIT @ {tp1_trig} (+{net_pct:g}% net + {fee_pct:g}% fees"
                f"{f' + recover {rec:.2f}%' if rec > 0 else ''}){grid.RESET}"
            )
            return

    # Replace only our TP1 — never wipe BE / trail
    staged.cancel_our_algos(sym, "TP1", api, sec, recv)

    if staged.profit_target_hit(entry, mark, side_is_long, profit_pct):
        print(
            f"{grid.YELLOW}Partial TP already hit (mark) — "
            f"{close_side} MARKET {tp1_str} ({partial:g}%){grid.RESET}"
        )
        runner_qty = qty
        if not dry:
            staged._market_reduce_qty(
                sym, side_is_long, tp1_d, hedge, filt, api, sec, recv,
            )
            refreshed = grid._detect_open_side(
                sym, hedge, api, sec, recv, prefer_is_long=side_is_long,
            )
            if refreshed[0] is None or refreshed[1] <= 0:
                state.update({
                    "partial_tp_armed": False,
                    "partial_tp_filled": True,
                    "partial_tp_qty": float(tp1_d),
                    "partial_tp_price": tp1_trig_f,
                    "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
                    "sl_paused_after_partial": True,
                })
                staged.save_state(sym, state)
                return
            runner_qty = refreshed[1]
            entry = refreshed[2] or entry
        state.update({
            "partial_tp_qty": float(tp1_d),
            "partial_tp_price": tp1_trig_f,
            "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
        })
        _on_partial_tp_fill(
            sym, side_is_long, runner_qty, entry, args, hedge, api, sec, filt, state,
            tp1_qty=float(tp1_d), partial_pct=partial,
        )
        return

    print(
        f"{close_side} TAKE_PROFIT_MARKET {tp1_str} ({partial:g}%) @ {tp1_trig} "
        f"(+{net_pct:g}% net + {fee_pct:g}% fees"
        f"{f' + recover {rec:.2f}%' if rec > 0 else ''}"
        f" · notional {notional:,.0f} USDT ≥ {thr_label})"
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
            refreshed = grid._detect_open_side(
                sym, hedge, api, sec, recv, prefer_is_long=side_is_long,
            )
            if refreshed[0] is None or refreshed[1] <= 0:
                state.update({
                    "partial_tp_armed": False,
                    "partial_tp_filled": True,
                    "partial_tp_qty": float(tp1_d),
                    "partial_tp_price": tp1_trig_f,
                    "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
                    "sl_paused_after_partial": True,
                })
                staged.save_state(sym, state)
                return
            state.update({
                "partial_tp_qty": float(tp1_d),
                "partial_tp_price": tp1_trig_f,
                "partial_tp_armed_qty": float(grid._round_to(qty, step, ROUND_DOWN)),
            })
            _on_partial_tp_fill(
                sym, side_is_long, refreshed[1], refreshed[2] or entry,
                args, hedge, api, sec, filt, state,
                tp1_qty=float(tp1_d), partial_pct=partial,
            )
            return
        raise

    print(
        f"{grid.GREEN}✓ Partial TP algoId={resp.get('algoId')} "
        f"(+{net_pct:g}% net + {fee_pct:g}% fees / {partial:g}% · ≥ {thr_label}){grid.RESET}"
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
