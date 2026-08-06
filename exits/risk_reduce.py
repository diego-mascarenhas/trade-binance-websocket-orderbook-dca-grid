"""Orthogonal risk-reduce addon: partial cut above HTF swing high + far full SL.

SHORT: STOP_MARKET BUY reduce-only
  · Partial (tag RR): max(swing_high × (1 + buffer), above DCA grid top)
  · Full    (tag RF): swing_high × (1 + RISK_FULL_BUFFER_PCT) — catastrophe

Swing high = max high of the last RISK_REDUCE_SWING_BARS daily candles (default 120).
RR is never placed inside the open ask DCA ladder.

After the partial fills:
  · cancel DCA (re-arm above only if still ★-like)
  · store recovery_pct so structure/BE require the runner to cover RR loss
  · full SL stays synced to remaining size

Env / CLI:
  RISK_REDUCE=1
  RISK_REDUCE_PCT=50
  RISK_REDUCE_BUFFER_PCT=0.8
  RISK_FULL_BUFFER_PCT=48
  RISK_REDUCE_SWING_BARS=120
  RISK_REDUCE_IDEAL_NEAR=90
"""

from __future__ import annotations

import argparse
import os
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

TAG_PARTIAL = "RR"
TAG_FULL = "RF"
DEFAULT_SWING_BARS = 120


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "risk_reduce", None) is not None:
        return bool(args.risk_reduce)
    return _env_bool("RISK_REDUCE", True)


def reduce_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "risk_reduce_pct", None)
    if v is not None:
        return max(5.0, min(95.0, float(v)))
    return max(5.0, min(95.0, _env_float("RISK_REDUCE_PCT", 50.0)))


def reduce_buffer_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "risk_reduce_buffer_pct", None)
    if v is not None:
        return max(0.05, float(v))
    return max(0.05, _env_float("RISK_REDUCE_BUFFER_PCT", 0.8))


def full_buffer_pct(args: argparse.Namespace) -> float:
    """0 = disable full SL (only partial)."""
    v = getattr(args, "risk_full_buffer_pct", None)
    if v is not None:
        return max(0.0, float(v))
    return max(0.0, _env_float("RISK_FULL_BUFFER_PCT", 48.0))


def swing_bars(args: argparse.Namespace | None = None) -> int:
    """Daily bars for HTF swing high (default 120 ≈ 4 months)."""
    if args is not None:
        v = getattr(args, "risk_reduce_swing_bars", None)
        if v is not None:
            return max(14, int(v))
    return max(14, _env_int("RISK_REDUCE_SWING_BARS", DEFAULT_SWING_BARS))


def ideal_near_for_rearm(args: argparse.Namespace | None = None) -> float:
    if args is not None:
        v = getattr(args, "risk_reduce_ideal_near", None)
        if v is not None:
            return float(v)
    return _env_float("RISK_REDUCE_IDEAL_NEAR", 90.0)


def allow_dca_rearm(symbol: str) -> bool:
    """False after a risk-reduce cut when the setup is no longer ★-like."""
    try:
        import orderbook_staged_exit as staged

        st = staged.load_state(symbol.upper())
        return not bool(st.get("risk_block_rearm"))
    except Exception:
        return True


def recovery_pct_for(symbol: str) -> float:
    """Min runner profit %% required to cover RR loss (0 if inactive)."""
    try:
        import orderbook_staged_exit as staged

        st = staged.load_state(symbol.upper())
        if not bool(st.get("risk_recovery_active")):
            return 0.0
        return max(0.0, float(st.get("risk_recovery_pct") or 0))
    except Exception:
        return 0.0


def fetch_impulse_high(symbol: str, *, bars: int = DEFAULT_SWING_BARS) -> float | None:
    """Max high of the last ``bars`` daily candles (HTF swing)."""
    bars = max(14, int(bars))
    try:
        from futures_scan import FAPI_BASE, fetch_klines

        kl = fetch_klines(FAPI_BASE, symbol.upper(), "1d", max(bars + 2, 130))
    except Exception:
        return None
    if not kl:
        return None
    highs: list[float] = []
    for row in kl[-bars:]:
        try:
            highs.append(float(row[2]))
        except (TypeError, ValueError, IndexError):
            continue
    return max(highs) if highs else None


def near_high_pct(
    symbol: str,
    *,
    bars: int = DEFAULT_SWING_BARS,
    args: argparse.Namespace | None = None,
) -> float | None:
    """last / impulse_high × 100 (vs HTF swing)."""
    if args is not None:
        bars = swing_bars(args)
    bars = max(14, int(bars))
    try:
        from futures_scan import FAPI_BASE, fetch_klines

        kl = fetch_klines(FAPI_BASE, symbol.upper(), "1d", max(bars + 2, 130))
    except Exception:
        return None
    if not kl:
        return None
    try:
        last = float(kl[-1][4])
    except (TypeError, ValueError, IndexError):
        return None
    peak = fetch_impulse_high(symbol, bars=bars)
    if not peak or peak <= 0 or last <= 0:
        return None
    return last / peak * 100.0


def still_suggested(symbol: str, args: argparse.Namespace | None = None) -> bool:
    near = near_high_pct(symbol, args=args)
    if near is None:
        return False
    return near >= ideal_near_for_rearm(args)


def dca_grid_top(
    symbol: str,
    api: str,
    sec: str,
    recv: int,
    *,
    entry: float = 0.0,
) -> float:
    """Highest open obdca* limit price (SHORT ask ladder), else entry."""
    import orderbook_dca_grid as grid

    top = float(entry or 0)
    try:
        oo = grid._signed_request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()}, api, sec, recv,
        ) or []
    except Exception:
        return top
    for o in oo if isinstance(oo, list) else []:
        cid = grid._order_client_id(o)
        if not cid.startswith("obdca"):
            continue
        try:
            px = float(o.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if px > top:
            top = px
    return top


def _triggers(
    impulse_high: float,
    is_long: bool,
    args: argparse.Namespace,
    *,
    rr_floor: float = 0.0,
) -> tuple[float, float | None]:
    """Return (partial_trigger, full_trigger_or_None)."""
    rb = reduce_buffer_pct(args) / 100.0
    fb = full_buffer_pct(args) / 100.0
    if is_long:
        partial = impulse_high * (1.0 - rb)
        full = impulse_high * (1.0 - fb) if fb > 0 else None
    else:
        partial = impulse_high * (1.0 + rb)
        if rr_floor > 0:
            partial = max(partial, rr_floor)
        full = impulse_high * (1.0 + fb) if fb > 0 else None
        if full is not None and full <= partial:
            # Keep catastrophe SL strictly above the partial cut
            full = partial * (1.0 + max(fb, 0.05))
    return partial, full


def _ensure_stop(
    symbol: str,
    is_long: bool,
    qty: float,
    trigger: float,
    tag: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    dry: bool,
) -> dict[str, Any] | None:
    """Place/resync STOP_MARKET with tag RR or RF. Returns algo dict or None."""
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return None
    qty_str = f"{qty_d:.{qty_dp}f}"
    trig_d = (
        grid._round_to(trigger, tick, ROUND_UP)
        if not is_long
        else grid._round_to(trigger, tick, ROUND_DOWN)
    )
    trig_str = f"{float(trig_d):.{price_dp}f}"
    trig_f = float(trig_d)

    existing = staged.find_our_algo(symbol, tag, api, sec, recv)
    if existing and staged._algo_qty_matches(existing, qty_str, step):
        try:
            et = float(existing.get("triggerPrice", 0) or 0)
        except (TypeError, ValueError):
            et = 0.0
        if abs(et - trig_f) <= float(tick) * 1.5:
            return existing

    if existing:
        staged.cancel_our_algos(symbol, tag, api, sec, recv)

    if dry:
        return {"dry_run": True, "triggerPrice": trig_str, "quantity": qty_str}

    try:
        mark = staged.get_mark_price(symbol, api, sec, recv)
    except Exception:
        mark = 0.0
    if mark > 0 and staged._stop_would_immediately_trigger(is_long, trig_f, mark, tick):
        print(
            f"{grid.YELLOW}Risk {tag} @ {grid.price_fmt(trig_f)} would trigger now "
            f"(mark {grid.price_fmt(mark)}) — skip place{grid.RESET}"
        )
        return None

    try:
        return staged.place_algo_order(
            symbol, is_long, "STOP_MARKET", qty_str, trig_str,
            hedge, api, sec, recv, client_tag=tag,
        )
    except Exception as exc:
        print(f"{grid.RED}Risk {tag} place failed: {exc}{grid.RESET}")
        return None


def sync_flat(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    staged.cancel_our_algos(symbol, TAG_PARTIAL, api, sec, recv)
    staged.cancel_our_algos(symbol, TAG_FULL, api, sec, recv)
    st = staged.load_state(symbol.upper())
    for k in (
        "risk_reduce_armed",
        "risk_reduce_filled",
        "risk_impulse_high",
        "risk_partial_trig",
        "risk_full_trig",
        "risk_armed_qty",
        "risk_block_rearm",
        "risk_tg_suggested",
        "risk_partial_loss_usdt",
        "risk_recovery_pct",
        "risk_recovery_active",
        "risk_reduce_pct",
    ):
        st.pop(k, None)
    staged.save_state(symbol.upper(), st)


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
    if not enabled(args):
        return
    # Pumpstall / this addon targets SHORT invalidation above impulse high.
    if side_is_long:
        return
    if entry <= 0 or qty <= 0:
        return

    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    dry = bool(getattr(args, "dry_run", False))
    sym = symbol.upper()
    step = filt["step_size"]
    state = staged.load_state(sym)
    side = "SHORT"
    bars = swing_bars(args)

    # Detect partial fill: we had RR armed, RR gone, qty shrank vs armed
    rr_open = staged.find_our_algo(sym, TAG_PARTIAL, api, sec, recv)
    armed_qty = float(state.get("risk_armed_qty", 0) or 0)
    was_armed = bool(state.get("risk_reduce_armed"))
    already_filled = bool(state.get("risk_reduce_filled"))

    if was_armed and not already_filled and rr_open is None and armed_qty > 0:
        if qty < armed_qty - float(step) / 2:
            _on_partial_filled(
                sym, side_is_long, qty, entry, armed_qty, args, hedge, api, sec, filt, state,
            )
            state = staged.load_state(sym)
            already_filled = True

    grid_top = dca_grid_top(sym, api, sec, recv, entry=entry)
    rb = reduce_buffer_pct(args) / 100.0
    rr_floor = grid_top * (1.0 + rb) if grid_top > 0 else 0.0

    # Freeze HTF swing on first arm; upgrade if frozen high is below grid or below live {bars}d peak
    impulse = float(state.get("risk_impulse_high", 0) or 0)
    prev_impulse = impulse
    need_refresh = impulse <= 0
    if impulse > 0 and grid_top > 0 and impulse < grid_top:
        need_refresh = True
        print(
            f"{grid.YELLOW}Risk-reduce: frozen high {grid.price_fmt(impulse)} "
            f"< grid top {grid.price_fmt(grid_top)} — re-fetch {bars}d swing{grid.RESET}"
        )
    fetched = fetch_impulse_high(sym, bars=bars)
    if not fetched or fetched <= 0:
        if need_refresh or impulse <= 0:
            print(f"{grid.DIM}Risk-reduce: no impulse high for {sym}{grid.RESET}")
            return
    else:
        fetched_f = float(fetched)
        if impulse <= 0 or need_refresh:
            impulse = fetched_f
        elif fetched_f > impulse * 1.0001:
            # Live HTF peak higher than old freeze (e.g. migrate 14d → 120d)
            print(
                f"{grid.CYAN}Risk-reduce: upgrade swing {grid.price_fmt(impulse)} → "
                f"{grid.price_fmt(fetched_f)} ({bars}d){grid.RESET}"
            )
            impulse = fetched_f
        state["risk_impulse_high"] = impulse

    partial_trig, full_trig = _triggers(
        impulse, side_is_long, args, rr_floor=rr_floor,
    )
    # Re-announce on Telegram if swing/trigger moved after first suggest (migration / grid floor)
    prev_partial = float(state.get("risk_partial_trig", 0) or 0)
    if (
        bool(state.get("risk_tg_suggested"))
        and (
            (prev_impulse > 0 and impulse > prev_impulse * 1.0001)
            or (prev_partial > 0 and partial_trig > prev_partial * 1.0001)
        )
    ):
        state.pop("risk_tg_suggested", None)
        staged.save_state(sym, state)
    pct = reduce_pct(args)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return

    if already_filled:
        # Only keep/sync the far full SL on the runner
        if full_trig is not None and full_trig > 0:
            resp = _ensure_stop(
                sym, side_is_long, float(qty_d), full_trig, TAG_FULL,
                args, hedge, api, sec, filt, dry=dry,
            )
            if resp is not None:
                state["risk_full_trig"] = full_trig
                staged.save_state(sym, state)
                print(
                    f"{grid.DIM}Risk full-SL sync · {side} qty={float(qty_d):g} @ "
                    f"{grid.price_fmt(full_trig)} (+{full_buffer_pct(args):g}% "
                    f"over high {grid.price_fmt(impulse)}){grid.RESET}"
                )
        return

    # Partial qty
    cut_d = grid._round_to(float(qty_d) * pct / 100.0, step, ROUND_DOWN)
    remain_d = qty_d - cut_d
    min_qty = filt["min_qty"]
    if cut_d < min_qty or remain_d < min_qty:
        print(f"{grid.DIM}Risk-reduce skip · qty too small to split {pct:g}%{grid.RESET}")
        return

    first_arm = not was_armed
    rr = _ensure_stop(
        sym, side_is_long, float(cut_d), partial_trig, TAG_PARTIAL,
        args, hedge, api, sec, filt, dry=dry,
    )
    if full_trig is not None and full_trig > partial_trig:
        _ensure_stop(
            sym, side_is_long, float(qty_d), full_trig, TAG_FULL,
            args, hedge, api, sec, filt, dry=dry,
        )

    above_grid = rr_floor > 0 and partial_trig + 1e-12 >= rr_floor
    state.update({
        "risk_reduce_armed": True,
        "risk_reduce_filled": False,
        "risk_impulse_high": impulse,
        "risk_partial_trig": partial_trig,
        "risk_full_trig": full_trig,
        "risk_armed_qty": float(qty_d),
        "risk_reduce_pct": pct,
    })
    staged.save_state(sym, state)

    # First arm, or re-suggest after swing/trigger upgrade (risk_tg_suggested cleared above)
    should_announce = rr is not None and (
        first_arm or not bool(state.get("risk_tg_suggested"))
    )
    if should_announce:
        print(
            f"{grid.BOLD}{grid.CYAN}✓ Risk-reduce · {side} cut {pct:g}% @ "
            f"{grid.price_fmt(partial_trig)} "
            f"(swing {bars}d high {grid.price_fmt(impulse)} "
            f"+{reduce_buffer_pct(args):g}%"
            + (
                f" · ≥ grid top {grid.price_fmt(grid_top)}"
                if above_grid and grid_top > 0
                else ""
            )
            + ")"
            + (
                f" · full SL @ {grid.price_fmt(full_trig)} "
                f"(+{full_buffer_pct(args):g}%)"
                if full_trig
                else ""
            )
            + f"{grid.RESET}"
        )
        _telegram_suggest(
            sym, side, float(qty_d), entry, impulse, partial_trig, full_trig, pct, args,
            hedge, api, sec, recv, grid_top=grid_top,
        )
    else:
        print(
            f"{grid.DIM}Risk-reduce sync · {side} cut={float(cut_d):g} @ "
            f"{grid.price_fmt(partial_trig)}"
            + (f" · full @ {grid.price_fmt(full_trig)}" if full_trig else "")
            + f" · swing {bars}d{grid.RESET}"
        )


def _on_partial_filled(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    armed_qty: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    state: dict[str, Any],
) -> None:
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    sym = symbol.upper()
    closed = max(0.0, armed_qty - qty)
    impulse = float(state.get("risk_impulse_high", 0) or 0)
    partial_trig = float(state.get("risk_partial_trig", 0) or 0)
    full_trig = state.get("risk_full_trig")
    pct = float(state.get("risk_reduce_pct", reduce_pct(args)) or reduce_pct(args))

    # Runner must recover RR loss before structure/BE can flatten
    fill = partial_trig if partial_trig > 0 else entry
    if side_is_long:
        loss_usdt = max(0.0, closed * (entry - fill))
    else:
        loss_usdt = max(0.0, closed * (fill - entry))
    remain = max(float(qty), 0.0)
    if remain > 0 and entry > 0 and loss_usdt > 0:
        recovery_pct = loss_usdt / (remain * entry) * 100.0
    else:
        recovery_pct = 0.0

    suggested = still_suggested(sym, args)
    state["risk_reduce_filled"] = True
    state["risk_block_rearm"] = not suggested
    state["risk_partial_loss_usdt"] = float(loss_usdt)
    state["risk_recovery_pct"] = float(recovery_pct)
    state["risk_recovery_active"] = recovery_pct > 0
    staged.save_state(sym, state)

    # Drop DCA so supervise can re-arm above (shorts) if still ★-like
    try:
        n = staged.cancel_dca_grid_orders(sym, api, sec, recv)
        if n:
            print(f"{grid.YELLOW}Risk-reduce filled → cancelled {n} DCA (re-arm check){grid.RESET}")
    except Exception as exc:
        print(f"{grid.YELLOW}Risk-reduce DCA cancel skipped: {exc}{grid.RESET}")

    recover_note = (
        f" · runner needs ≥+{recovery_pct:.2f}% to recover RR ({loss_usdt:.2f} USDT)"
        if recovery_pct > 0
        else ""
    )
    if suggested:
        print(
            f"{grid.BOLD}{grid.GREEN}✓ Risk-reduce filled · closed ~{closed:g} "
            f"({pct:g}%) · runner {qty:g} · setup still near high → re-arm DCA"
            f"{recover_note}{grid.RESET}"
        )
    else:
        print(
            f"{grid.BOLD}{grid.YELLOW}✓ Risk-reduce filled · closed ~{closed:g} "
            f"({pct:g}%) · runner {qty:g} · setup no longer ★ → no DCA re-arm"
            f"{recover_note}{grid.RESET}"
        )

    try:
        import telegram_notify as telegram

        lev = grid.get_symbol_leverage(sym, api, sec, recv)
        _, upnl = staged.position_pnl(sym, side_is_long, hedge, api, sec, recv)
        telegram.notify_risk_reduce_filled(
            sym, "SHORT",
            closed_qty=closed,
            remain_qty=qty,
            entry=entry,
            trigger=partial_trig,
            impulse_high=impulse,
            full_sl=float(full_trig) if full_trig else None,
            rearm=suggested,
            leverage=lev,
            pnl_usdt=upnl,
        )
    except Exception:
        pass

    # Sync full SL to runner qty immediately
    if full_trig and float(full_trig) > 0 and qty > 0:
        _ensure_stop(
            sym, side_is_long, qty, float(full_trig), TAG_FULL,
            args, hedge, api, sec, filt, dry=bool(getattr(args, "dry_run", False)),
        )


def _telegram_suggest(
    symbol: str,
    side: str,
    qty: float,
    entry: float,
    impulse: float,
    partial_trig: float,
    full_trig: float | None,
    pct: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    grid_top: float = 0.0,
) -> None:
    import orderbook_staged_exit as staged

    st = staged.load_state(symbol)
    if bool(st.get("risk_tg_suggested")):
        return
    try:
        import orderbook_dca_grid as grid
        import telegram_notify as telegram

        lev = grid.get_symbol_leverage(symbol, api, sec, recv)
        _, upnl = staged.position_pnl(symbol, side == "LONG", hedge, api, sec, recv)
        telegram.notify_risk_reduce_armed(
            symbol, side, qty, entry,
            impulse_high=impulse,
            partial_sl=partial_trig,
            full_sl=full_trig,
            reduce_pct=pct,
            reduce_buffer_pct=reduce_buffer_pct(args),
            full_buffer_pct=full_buffer_pct(args),
            leverage=lev,
            pnl_usdt=upnl,
            swing_bars=swing_bars(args),
            grid_top=grid_top if grid_top > 0 else None,
        )
        st["risk_tg_suggested"] = True
        staged.save_state(symbol, st)
    except Exception:
        pass
