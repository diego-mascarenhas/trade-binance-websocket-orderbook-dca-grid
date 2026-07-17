#!/usr/bin/env python3
"""OB scalp bot — synthetic bars from order-book depth, market entries.

Separate from orderbook_dca_grid.py (no grid, no staged exit). Polls depth via
REST, builds internal bars (default 60s), enters/exits with MARKET orders.

Usage:
    python3 orderbook_ob_scalp.py BTCUSDT --dry-run
    python3 orderbook_ob_scalp.py HEIUSDT --execute --bar-sec 60
    python3 orderbook_ob_scalp.py SOLUSDT --execute --bar-sec 15 --sample-sec 1
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from ob_bars import BarBuilder, depth_to_levels
from ob_ema import (
    append_ema_log,
    ema_allows,
    fetch_ema_snapshot,
    format_ema_console,
)
from ob_pattern import (
    PatternConfig,
    evaluate_pattern,
    format_pattern_console,
    pattern_allows,
)
from ob_structure import (
    StructureConfig,
    fetch_structure,
    format_structure_console,
)
from ob_oscillators import (
    OscillatorConfig,
    fetch_oscillators,
    format_oscillators_console,
)
from ob_triggers import (
    collect_triggers,
    hit_sl,
    hit_tp,
    resolve_ob_exits,
)
from ob_scalp_ml import feature_vector, load_models, predict_prob
from ob_scalp_adaptive import (
    effective_filters,
    format_adaptive_line,
    load_adaptive,
    maybe_relax_inactivity,
    on_trade_open,
)
from ob_scalp_learn import effective_sl_pct, register_close_watch, tick_outcome_watches
from ob_scalp_dataset import BarRecord, append_bar
from ob_scalp_pnl import format_pnl_line, load_pnl_stats, refresh_pnl_stats
from ob_scalp_recovery import (
    RecoveryState,
    append_journal,
    ensure_base_notional,
    format_status,
    load_state,
    maybe_expire_side_lock,
    pnl_usdt,
    record_close,
    recovery_covers_debt,
    reset_state,
    save_state,
    target_notional,
)
from ob_signals import (
    SignalConfig,
    entry_signal,
    exit_on_flip,
    profit_pct,
    should_discretionary_close,
    should_tp_close,
)
from trade_sounds import play_close_sound, play_sound, sound_pack_label, sounds_enabled
from ob_scalp_stack import clear_drain, is_draining, stop_watch
from ob_scalp_exits import (
    cancel_scalp_exchange_exits,
    our_exits_present,
    place_scalp_exchange_exits,
)

from orderbook_dca_grid import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    _dec_places,
    _detect_open_side,
    _resolve_hedge,
    _round_to,
    _signed_request,
    fetch_depth,
    get_position,
    get_wallet_balance,
    load_env_file,
    load_keys,
    load_symbol_filters,
    market_close_position,
    price_fmt,
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def client_id(symbol: str, tag: str) -> str:
    sym = symbol.upper()
    ts = int(time.time()) % 1_000_000
    return f"obscalp{tag}{sym}{ts}"[:36]


def market_open(
    symbol: str,
    is_long: bool,
    qty_str: str,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    cid: str,
) -> dict[str, Any]:
    side = "BUY" if is_long else "SELL"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": cid,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    return _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def qty_exchange_min(price: float, filt: dict[str, Decimal]) -> tuple[str, float]:
    """Smallest valid qty: LOT_SIZE min_qty, bumped to MIN_NOTIONAL if needed."""
    step = filt["step_size"]
    qty_dp = _dec_places(step)
    if price <= 0:
        raise ValueError("invalid price for sizing")
    qty_d = filt["min_qty"]
    while qty_d * Decimal(str(price)) < filt["min_notional"]:
        qty_d += step
    qty_str = f"{qty_d:.{qty_dp}f}"
    return qty_str, float(qty_d)


def qty_for_notional(
    notional: float,
    price: float,
    filt: dict[str, Decimal],
) -> tuple[str, float]:
    step = filt["step_size"]
    tick = filt["tick_size"]
    qty_dp = _dec_places(step)
    if price <= 0:
        raise ValueError("invalid price for sizing")
    qty_d = _round_to(notional / price, step, ROUND_DOWN)
    if qty_d < filt["min_qty"]:
        qty_d = filt["min_qty"]
    while qty_d * Decimal(str(price)) < filt["min_notional"]:
        qty_d += step
    qty_str = f"{qty_d:.{qty_dp}f}"
    return qty_str, float(qty_d)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_trig(name: str, default: bool = True) -> bool:
    """Trigger enable flag (default on unless explicitly disabled)."""
    return _env_bool(name, default)


def resolve_entry_qty(
    args: argparse.Namespace,
    price: float,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
    *,
    recovery: RecoveryState | None = None,
) -> tuple[str, float, float]:
    """Return (qty_str, qty_float, notional_usdt)."""
    base_qty_str, base_qty_f = _base_entry_qty(args, price, filt, api, sec)
    base_notional = price * base_qty_f
    if not args.recover or recovery is None:
        return base_qty_str, base_qty_f, base_notional

    ensure_base_notional(recovery, base_notional)
    level = min(recovery.level, args.recover_max_level)
    notional = recovery.base_notional_usdt * (2 ** level)
    qty_str, qty_f = qty_for_notional(notional, price, filt)
    return qty_str, qty_f, notional


def _base_entry_qty(
    args: argparse.Namespace,
    price: float,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
) -> tuple[str, float]:
    mode = getattr(args, "size_mode", "min")
    if mode == "min":
        return qty_exchange_min(price, filt)
    notional = args.base_size
    if notional <= 0:
        bal = get_wallet_balance(api, sec, args.recv_window)
        notional = bal * args.wallet_pct / 100.0
    return qty_for_notional(notional, price, filt)


def size_mode_summary(args: argparse.Namespace, filt: dict[str, Decimal], price: float) -> str:
    if getattr(args, "size_mode", "min") == "min":
        try:
            qty_str, _ = qty_exchange_min(price, filt)
            return f"exchange min ({qty_str})"
        except ValueError:
            return "exchange min"
    if args.base_size > 0:
        return f"{args.base_size:g} USDT fixed"
    return f"{args.wallet_pct:g}% wallet"


def _dca_supervisor_running(symbol: str) -> bool:
    try:
        import botctl

        return botctl.is_running(symbol.upper())
    except Exception:
        return False


def _side_color(signal: str) -> str:
    return GREEN if signal.lower() == "long" else RED


def _side_label(is_long: bool) -> str:
    direction = "LONG" if is_long else "SHORT"
    return f"{_side_color('long' if is_long else 'short')}{direction}{RESET}"


def _tp_sl_prices(entry: float, is_long: bool, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    if is_long:
        return entry * (1 + tp_pct / 100), entry * (1 - sl_pct / 100)
    return entry * (1 - tp_pct / 100), entry * (1 + sl_pct / 100)


def _trail_stop_price(pos: PositionState, trail_pct: float) -> float:
    if pos.is_long:
        return pos.extreme_mark * (1 - trail_pct / 100)
    return pos.extreme_mark * (1 + trail_pct / 100)


def _update_trail(pos: PositionState, mark: float, args: argparse.Namespace) -> None:
    if args.trail_pct <= 0:
        return
    if pos.is_long:
        pos.extreme_mark = max(pos.extreme_mark, mark)
    else:
        pos.extreme_mark = min(pos.extreme_mark, mark) if pos.extreme_mark > 0 else mark
    pnl = profit_pct(pos.entry, mark, pos.is_long)
    if not pos.trail_armed and pnl >= args.trail_arm_pct:
        pos.trail_armed = True


def _trail_triggered(pos: PositionState, mark: float, trail_pct: float) -> bool:
    if not pos.trail_armed or trail_pct <= 0:
        return False
    stop = _trail_stop_price(pos, trail_pct)
    if pos.is_long:
        return mark <= stop
    return mark >= stop


def _print_pnl_summary(
    sym: str,
    pos: PositionState | None,
    mark: float,
) -> None:
    stats = load_pnl_stats(sym)
    unrealized = None
    if pos is not None:
        unrealized = _estimated_net_usdt(pos.entry, mark, pos.qty, pos.is_long, 0.08)
    print(format_pnl_line(stats, unrealized_usdt=unrealized, compact=False))


def _print_open_position(pos: PositionState, mark: float, args: argparse.Namespace) -> None:
    pnl = profit_pct(pos.entry, mark, pos.is_long)
    if pos.tp_price > 0 and pos.sl_price > 0:
        tp_px, sl_px = pos.tp_price, pos.sl_price
    else:
        tp_px, sl_px = _tp_sl_prices(pos.entry, pos.is_long, args.tp_pct, args.sl_pct)
    trail_note = ""
    if args.trail_pct > 0:
        if pos.trail_armed:
            trail_note = f"  {GREEN}trail{RESET} stop {price_fmt(_trail_stop_price(pos, args.trail_pct))}"
        else:
            trail_note = f"  {DIM}trail arms @ +{args.trail_arm_pct:g}%{RESET}"
    trig = f"  {DIM}via {pos.trigger}{RESET}" if pos.trigger else ""
    print(
        f"  {_side_label(pos.is_long)} @ {price_fmt(pos.entry)}  "
        f"pnl {pnl:+.3f}%  TP {price_fmt(tp_px)}  SL {price_fmt(sl_px)}{trail_note}{trig}",
    )


def _print_close_event(
    reason: str,
    pos: PositionState,
    gross_pct: float,
    exit_price: float,
    *,
    fee_buffer: float,
) -> None:
    net = gross_pct - fee_buffer
    side = _side_label(pos.is_long)
    net_color = GREEN if net > 0 else RED
    if reason == "TP":
        print(
            f"{GREEN}TP hit{RESET} {side} gross {gross_pct:+.3f}% "
            f"(est. net {net_color}{net:+.3f}%{RESET}) @ {price_fmt(exit_price)}",
        )
    elif reason == "SL":
        print(
            f"{RED}SL hit{RESET} {side} {gross_pct:+.3f}% "
            f"(est. net {net_color}{net:+.3f}%{RESET}) @ {price_fmt(exit_price)}",
        )
    elif reason == "TRAIL":
        label = f"{GREEN}Trail stop{RESET}" if net > 0 else f"{RED}Trail stop{RESET}"
        print(
            f"{label} {side} gross {gross_pct:+.3f}% "
            f"(est. net {net_color}{net:+.3f}%{RESET}) @ {price_fmt(exit_price)}",
        )
    elif reason == "FLIP":
        print(
            f"{YELLOW}Imbalance flip → exit{RESET} {side} gross {gross_pct:+.3f}% "
            f"(est. net {net_color}{net:+.3f}%{RESET}) @ {price_fmt(exit_price)}",
        )
    else:
        print(
            f"{YELLOW}{reason} → exit{RESET} {side} gross {gross_pct:+.3f}% "
            f"(est. net {net_color}{net:+.3f}%{RESET}) @ {price_fmt(exit_price)}",
        )


def print_bar(bar: Any, signal: str | None) -> None:
    if signal:
        color = _side_color(signal)
        sig = f"  {color}→ {signal.upper()}{RESET}"
    else:
        sig = ""
    print(
        f"{DIM}bar {time.strftime('%H:%M:%S', time.localtime(bar.t_close))}{RESET}  "
        f"mid {price_fmt(bar.mid_c)} ({bar.mid_change_pct():+.3f}%)  "
        f"imb {bar.imbalance * 100:.1f}%  "
        f"walls bid {qty_fmt(bar.bid_wall_qty)} @ {price_fmt(bar.bid_wall_price)}  "
        f"ask {qty_fmt(bar.ask_wall_qty)} @ {price_fmt(bar.ask_wall_price)}  "
        f"[{bar.samples} samples]{sig}",
    )


def qty_fmt(qty: float) -> str:
    if qty >= 1000:
        return f"{qty:,.1f}"
    if qty >= 1:
        return f"{qty:,.2f}"
    return f"{qty:.4f}"


@dataclass
class PositionState:
    is_long: bool
    entry: float
    qty: float
    opened_at: float
    recovery_level: int = 0
    bars_held: int = 0
    extreme_mark: float = 0.0
    trail_armed: bool = False
    entry_features: list[float] | None = None
    entry_signal: str = ""
    trigger: str = ""
    tp_price: float = 0.0
    sl_price: float = 0.0
    exits_note: str = ""

    def __post_init__(self) -> None:
        if self.extreme_mark <= 0:
            self.extreme_mark = self.entry


def _estimated_net_usdt(
    entry: float,
    exit_price: float,
    qty: float,
    is_long: bool,
    fee_buffer_pct: float,
) -> float:
    gross = pnl_usdt(entry, exit_price, qty, is_long)
    notional = entry * qty
    return gross - notional * fee_buffer_pct / 100.0


def _recovery_allows_soft_exit(
    *,
    recover: bool,
    recovery: RecoveryState,
    entry: float,
    exit_price: float,
    qty: float,
    is_long: bool,
    fee_buffer: float,
) -> tuple[bool, str]:
    """While martingale debt remains, only soft-exit if this close would clear it."""
    if not recover or recovery.cumulative_loss_usdt <= 0:
        return True, ""
    net = _estimated_net_usdt(entry, exit_price, qty, is_long, fee_buffer)
    if recovery_covers_debt(net, recovery):
        return True, ""
    return (
        False,
        f"recovery hold — need ≥{recovery.cumulative_loss_usdt:.4f} USDT to clear debt "
        f"(est. net {net:+.4f})",
    )


def _arm_exchange_exits(
    sym: str,
    pos: PositionState,
    mark: float,
    args: argparse.Namespace,
    hedge: bool,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
    *,
    force: bool = False,
) -> None:
    """Place Binance TP/SL algo orders from pos.tp_price / pos.sl_price (OB or %)."""
    if args.dry_run or not getattr(args, "exchange_exits", True):
        return
    tp_px, sl_px = pos.tp_price, pos.sl_price
    if tp_px <= 0 or sl_px <= 0:
        tp_px, sl_px = _tp_sl_prices(pos.entry, pos.is_long, args.tp_pct, args.sl_pct)
        pos.tp_price, pos.sl_price = tp_px, sl_px
    if not force:
        has_tp, has_sl = our_exits_present(sym, api, sec, args.recv_window)
        if has_tp and has_sl:
            return
    try:
        tp_used, sl_used = place_scalp_exchange_exits(
            sym, pos.is_long, pos.qty, pos.entry, mark, tp_px, sl_px,
            filt, hedge, api, sec, args.recv_window,
            fee_buffer_pct=args.fee_buffer,
            replace=True,
        )
        pos.tp_price, pos.sl_price = tp_used, sl_used
        append_journal(
            sym,
            f"EXITS armed TP={tp_used:g} SL={sl_used:g} qty={pos.qty:g}",
        )
    except Exception as exc:
        print(f"{YELLOW}Exchange exits not armed: {exc}{RESET}")


def _handle_close(
    sym: str,
    pos: PositionState,
    exit_price: float,
    gross_pct: float,
    reason: str,
    args: argparse.Namespace,
    recovery: RecoveryState,
    hedge: bool,
    filt: dict[str, Decimal],
    api: str,
    sec: str,
    *,
    adaptive_state=None,
    already_flat: bool = False,
) -> float:
    """Close position; return monotonic close timestamp for entry cooldown."""
    direction = "LONG" if pos.is_long else "SHORT"
    net_usdt = _estimated_net_usdt(
        pos.entry, exit_price, pos.qty, pos.is_long, args.fee_buffer,
    )
    if args.recover:
        debt_before = recovery.cumulative_loss_usdt
        record_close(
            sym,
            recovery,
            reason=reason,
            direction=direction,
            entry=pos.entry,
            exit_price=exit_price,
            qty=pos.qty,
            gross_pct=gross_pct,
            net_usdt=net_usdt,
            dry_run=args.dry_run,
            trigger=pos.trigger or "",
        )
        if recovery.level <= 0 and recovery.cumulative_loss_usdt <= 0:
            print(f"{GREEN}Recovery reset — debt cleared, back to base size.{RESET}")
        elif net_usdt > 0:
            print(
                f"{YELLOW}Partial recovery {net_usdt:+.4f} USDT "
                f"(was {debt_before:.4f} → left {recovery.cumulative_loss_usdt:.4f}) · "
                f"level {recovery.level} ({recovery.multiplier:g}x) "
                f"locked {recovery.locked_side.upper()}{RESET}",
            )
        else:
            lock = f" · retry {recovery.locked_side.upper()} only" if recovery.locked_side else ""
            print(
                f"{YELLOW}Recovery level {recovery.level} ({recovery.multiplier:g}x){lock} · "
                f"cumulative loss {recovery.cumulative_loss_usdt:.4f} USDT{RESET}",
            )
    elif not args.dry_run:
        trig = f" trigger={pos.trigger}" if pos.trigger else ""
        append_journal(
            sym,
            f"{reason} {direction} qty={pos.qty:g} entry={pos.entry:g} exit={exit_price:g} "
            f"gross={gross_pct:+.3f}% pnl={net_usdt:+.4f} USDT{trig}",
        )

    if not args.dry_run:
        try:
            n = cancel_scalp_exchange_exits(sym, api, sec, args.recv_window)
            if n:
                print(f"{DIM}Cancelled {n} exchange exit algo(s){RESET}")
        except Exception as exc:
            print(f"{YELLOW}Cancel exchange exits: {exc}{RESET}")
        if not already_flat:
            market_close_position(sym, pos.is_long, pos.qty, hedge, filt, api, sec, args.recv_window)
    refresh_pnl_stats(sym)
    _print_pnl_summary(sym, None, exit_price)
    if not args.dry_run:
        # Always use estimated net (after fee buffer) — never assume TP/TRAIL = win
        play_close_sound(net_usdt)
    if args.adaptive and adaptive_state is not None and pos.entry_features:
        signal = pos.entry_signal or ("long" if pos.is_long else "short")
        watch_sec = max(args.bar_sec * 3, 120.0)
        eff_sl = effective_sl_pct(adaptive_state, args.sl_pct)
        msg = register_close_watch(
            sym,
            signal=signal,
            features=pos.entry_features,
            entry=pos.entry,
            exit_price=exit_price,
            is_long=pos.is_long,
            reason=reason,
            net_usdt=net_usdt,
            tp_pct=args.tp_pct,
            sl_pct=eff_sl,
            fee_buffer=args.fee_buffer,
            watch_sec=watch_sec,
        )
        print(f"{DIM}{msg}{RESET}")
    return time.time()


def _opposite_position_open(
    sym: str,
    is_long: bool,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> tuple[bool, float]:
    """True if the opposite hedge leg has size (one-way always false)."""
    if not hedge:
        return False, 0.0
    opp_qty, _ = get_position(sym, not is_long, hedge, api, sec, recv)
    return opp_qty > 0, opp_qty


def _fetch_depth_retry(symbol: str, limit: int, *, tries: int = 3, pause: float = 2.0) -> dict:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return fetch_depth(symbol, limit)
        except Exception as exc:
            last = exc
            if attempt + 1 < tries:
                print(f"{YELLOW}Depth timeout/error (retry {attempt + 2}/{tries}): {exc}{RESET}")
                time.sleep(pause)
    raise last  # type: ignore[misc]


def run_loop(args: argparse.Namespace) -> None:
    sym = args.symbol.upper()
    api, sec = load_keys(args.env_file)
    if not api or not sec:
        print(f"{RED}BINANCE_API_KEY / BINANCE_SECRET_KEY required.{RESET}", file=sys.stderr)
        sys.exit(1)

    if _dca_supervisor_running(sym) and not args.force:
        print(
            f"{RED}{sym} has an active DCA supervisor (botctl). "
            f"Stop it first or pass --force.{RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        filt = load_symbol_filters(sym)
    except Exception as exc:
        print(f"{RED}Symbol filters: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    hedge = _resolve_hedge(args, api, sec)
    if hedge:
        print(
            f"{YELLOW}⚠ Hedge mode ON — scalp expects one-way. "
            f"Use one-way account or --position-mode oneway to avoid LONG+SHORT stacks.{RESET}",
        )
    sig_cfg = SignalConfig(
        imb_long=args.imb_long,
        imb_short=args.imb_short,
        min_wall_qty=args.min_wall_qty,
        require_momentum=not args.no_momentum,
        momentum_min_pct=args.momentum_min_pct,
        use_imbalance=args.imb_filter,
    )
    builder = BarBuilder(bar_sec=args.bar_sec, band_pct=args.band_pct)
    recovery = reset_state(sym) if args.reset_recover else load_state(sym)
    if args.recover and not args.reset_recover:
        if maybe_expire_side_lock(
            sym, recovery, lock_min=args.recover_lock_min, dry_run=args.dry_run,
        ):
            print(
                f"{YELLOW}Recovery side-lock expired ({args.recover_lock_min:g}m) — "
                f"both sides allowed (size still {recovery.multiplier:g}x){RESET}",
            )
        append_journal(sym, f"START recover={recovery.level} cumulative={recovery.cumulative_loss_usdt:.4f}")

    try:
        _preview_bids, _preview_asks = depth_to_levels(fetch_depth(sym, args.limit))
        _preview_mid = (_preview_bids[0][0] + _preview_asks[0][0]) / 2 if _preview_bids and _preview_asks else 0.0
    except Exception:
        _preview_mid = 0.0
    size_note = size_mode_summary(args, filt, _preview_mid) if _preview_mid > 0 else args.size_mode

    mode = f"{YELLOW}DRY-RUN{RESET}" if args.dry_run else f"{GREEN}LIVE{RESET}"
    recover_note = ""
    if args.recover:
        recover_note = (
            f"\n  recover {format_status(recovery, lock_min=args.recover_lock_min)}  "
            f"max level {args.recover_max_level}  side-lock {args.recover_lock_min:g}m"
        )
        recover_note += f"\n  logs {Path('.run/logs') / sym}/scalp_trades.log"
    ema_note = ""
    if args.ema_filter:
        ema_note = (
            f"\n  EMA filter ON  {args.ema_fast}/{args.ema_slow} {args.ema_interval}  "
            f"slope≥{args.ema_slope_min:g}% over {args.ema_slope_bars} bars"
            f"\n  ema log {Path('.run/logs') / sym}/scalp_ema.log"
        )
    pat_note = ""
    if args.pattern_filter:
        pat_note = (
            f"\n  pattern filter ON  {args.pattern_interval}+{args.pattern_fvg_interval} "
            f"body≥{args.pattern_min_body_ratio:g} cont≥{args.pattern_min_continuity} "
            f"volx≥{args.pattern_min_vol_ratio:g} (ATR/BB/FVG)"
        )
    trail_note = ""
    if args.trail_pct > 0:
        trail_note = f"\n  trail stop {args.trail_pct:g}% after +{args.trail_arm_pct:g}% profit"
    sound_note = ""
    if sounds_enabled() and not args.no_sounds:
        pack = sound_pack_label()
        sound_note = f"\n  sounds ON ({pack}: entry/dca/tp/sl)"
    imb_note = ""
    if getattr(args, "multi_trigger", True):
        imb_note = (
            "\n  multi-trigger OR: choch · eql/eqh · rsi · stoch · htf · candles · momentum · imbalance · ema · ml"
            f"\n  (need ≥{getattr(args, 'trig_min_hits', 2)} agreeing · tagged in ./obscalp-trades)"
        )
    elif not args.imb_filter:
        imb_note = f"\n  entry signal: momentum≥{args.momentum_min_pct:g}% (imbalance filter OFF by default)"
    exits_note = ""
    if getattr(args, "ob_exits", True):
        exits_note = (
            f"\n  exits OB walls (fallback TP {args.tp_pct:g}% / SL {args.sl_pct:g}%, "
            f"max wall {getattr(args, 'ob_exit_max_pct', 3):g}%)"
        )
        if getattr(args, "exchange_exits", True):
            exits_note += "\n  exchange TP/SL ON (TAKE_PROFIT_MARKET + STOP_MARKET at open)"
        else:
            exits_note += "\n  exchange TP/SL OFF (software exits only)"
    if is_draining(sym):
        print(
            f"\n{YELLOW}DRAIN MODE · {sym} — manage open TP/SL only, no new entries; "
            f"exit when flat{RESET}",
        )
        append_journal(sym, "DRAIN mode active — exits only until flat")
    print(
        f"\n{BOLD}{CYAN}OB scalp · {sym}{RESET}  {mode}\n"
        f"  bar {args.bar_sec:g}s  sample {args.sample_sec:g}s  band ±{args.band_pct:g}%\n"
        + (
            f"  entry imb long≥{args.imb_long:.2f} short≤{args.imb_short:.2f}  "
            if not args.imb_filter
            else ""
        )
        + f"TP +{args.tp_pct:g}%  SL -{args.sl_pct:g}%  max {args.max_bars} bars\n"
        f"  fee buffer {args.fee_buffer:g}% (no TP/flip close if net ≤ 0)  "
        f"size {size_note}{imb_note}{exits_note}{recover_note}{ema_note}{pat_note}{trail_note}{sound_note}  Ctrl+C to stop\n",
    )
    refresh_pnl_stats(sym)
    _print_pnl_summary(sym, None, _preview_mid if _preview_mid > 0 else 0.0)

    pos: PositionState | None = None
    last_close_at: float = 0.0
    ml_model = load_models(sym) if args.ml_filter else None
    if ml_model:
        print(f"  {DIM}ML filter ON  min prob {args.ml_min_prob:.2f}{RESET}")
    bot_started_at = time.time()
    adaptive = load_adaptive(sym) if args.adaptive else None
    if args.adaptive and adaptive:
        eff0 = effective_filters(
            adaptive,
            base_ml=args.ml_min_prob,
            base_ema=args.ema_slope_min,
            base_imb_long=args.imb_long,
            base_imb_short=args.imb_short,
            base_momentum=args.momentum_min_pct,
        )
        print(f"  {DIM}Adaptive ON — {format_adaptive_line(adaptive, eff0, base_sl=args.sl_pct)}{RESET}")
    builder.start_bar(time.time())

    try:
        while True:
            now = time.time()
            ml_threshold = args.ml_min_prob
            ema_slope = args.ema_slope_min
            sl_threshold = args.sl_pct
            if args.adaptive:
                adaptive = load_adaptive(sym)
                relax_msg = maybe_relax_inactivity(sym, adaptive, bot_started_at=bot_started_at)
                if relax_msg:
                    print(f"{YELLOW}{relax_msg}{RESET}")
                eff = effective_filters(
                    adaptive,
                    base_ml=args.ml_min_prob,
                    base_ema=args.ema_slope_min,
                    base_imb_long=args.imb_long,
                    base_imb_short=args.imb_short,
                    base_momentum=args.momentum_min_pct,
                )
                ml_threshold = eff["ml_min_prob"]
                ema_slope = eff["ema_slope_min"]
                sl_threshold = effective_sl_pct(adaptive, args.sl_pct)
                if args.imb_filter:
                    sig_cfg.imb_long = eff["imb_long"]
                    sig_cfg.imb_short = eff["imb_short"]
                sig_cfg.momentum_min_pct = eff["momentum_min_pct"]
            try:
                depth = _fetch_depth_retry(sym, args.limit)
                bids, asks = depth_to_levels(depth)
            except Exception as exc:
                print(f"{RED}Depth fetch failed: {exc}{RESET}")
                time.sleep(args.sample_sec)
                continue

            if not bids or not asks:
                time.sleep(args.sample_sec)
                continue

            mark = (bids[0][0] + asks[0][0]) / 2

            if args.adaptive:
                for learn_msg in tick_outcome_watches(sym, mark):
                    print(f"{CYAN}{learn_msg}{RESET}")

            side, live_qty, live_entry = _detect_open_side(sym, hedge, api, sec, args.recv_window)
            if side is not None and live_qty > 0:
                if pos is None:
                    tp_px = sl_px = 0.0
                    exits_note = ""
                    if getattr(args, "ob_exits", True):
                        try:
                            ex = resolve_ob_exits(
                                bids, asks, live_entry, side,
                                fee_buffer=args.fee_buffer,
                                tick=filt["tick_size"],
                                tp_pct_fallback=args.tp_pct,
                                sl_pct_fallback=sl_threshold,
                                wall_min_mult=float(getattr(args, "ob_wall_min_mult", 1.0)),
                                max_range_pct=float(getattr(args, "ob_exit_max_pct", 3.0)),
                            )
                            tp_px, sl_px = ex.tp_price, ex.sl_price
                            exits_note = ex.note
                            print(f"{DIM}Adopted open position — exits OB · {ex.note}{RESET}")
                        except Exception as exc:
                            print(f"{YELLOW}Adopt OB exits failed: {exc}{RESET}")
                    pos = PositionState(
                        is_long=side,
                        entry=live_entry,
                        qty=live_qty,
                        opened_at=now,
                        tp_price=tp_px,
                        sl_price=sl_px,
                        exits_note=exits_note,
                        trigger="adopted",
                    )
                    _arm_exchange_exits(sym, pos, mark, args, hedge, filt, api, sec)
                else:
                    pos.qty = live_qty
                    pos.entry = live_entry
                    pos.is_long = side
            elif pos is not None and live_qty <= 0:
                pnl = profit_pct(pos.entry, mark, pos.is_long)
                reason = "TP" if pnl >= 0 else "SL"
                print(
                    f"{DIM}Position flat on exchange — recording {reason} "
                    f"(exchange exit or external close){RESET}",
                )
                last_close_at = _handle_close(
                    sym, pos, mark, pnl, reason, args, recovery, hedge, filt, api, sec,
                    adaptive_state=adaptive,
                    already_flat=True,
                )
                pos = None

            if is_draining(sym) and pos is None and (side is None or live_qty <= 0):
                print(
                    f"{GREEN}Drain complete · {sym} flat — shutting down "
                    f"(TP/SL handoff done){RESET}",
                )
                append_journal(sym, "DRAIN complete — flat, bot exit")
                clear_drain(sym)
                stop_watch(sym)
                return

            if pos is not None:
                pnl = profit_pct(pos.entry, mark, pos.is_long)
                _update_trail(pos, mark, args)
                use_ob_exits = bool(getattr(args, "ob_exits", True) and pos.tp_price > 0)
                tp_hit = False
                sl_hit = False
                if use_ob_exits:
                    tp_hit = hit_tp(mark, pos.is_long, pos.tp_price) and should_discretionary_close(
                        pnl, args.fee_buffer,
                    )
                    # Allow TP via wall even if % target not reached — still require net > 0
                    if hit_tp(mark, pos.is_long, pos.tp_price) and not should_discretionary_close(
                        pnl, args.fee_buffer,
                    ):
                        print(
                            f"{DIM}OB TP wall touched but est. net "
                            f"{pnl - args.fee_buffer:+.3f}% ≤ 0 — holding{RESET}",
                        )
                    sl_hit = hit_sl(mark, pos.is_long, pos.sl_price)
                else:
                    tp_hit = should_tp_close(pnl, args.tp_pct, args.fee_buffer)
                    sl_hit = pnl <= -sl_threshold

                if tp_hit:
                    _print_close_event("TP", pos, pnl, mark, fee_buffer=args.fee_buffer)
                    last_close_at = _handle_close(
                        sym, pos, mark, pnl, "TP", args, recovery, hedge, filt, api, sec,
                        adaptive_state=adaptive,
                    )
                    pos = None
                elif _trail_triggered(pos, mark, args.trail_pct) and should_discretionary_close(
                    pnl, args.fee_buffer,
                ):
                    ok, hold_msg = _recovery_allows_soft_exit(
                        recover=args.recover,
                        recovery=recovery,
                        entry=pos.entry,
                        exit_price=mark,
                        qty=pos.qty,
                        is_long=pos.is_long,
                        fee_buffer=args.fee_buffer,
                    )
                    if not ok:
                        print(f"{DIM}Trail armed but {hold_msg}{RESET}")
                    else:
                        _print_close_event("TRAIL", pos, pnl, mark, fee_buffer=args.fee_buffer)
                        last_close_at = _handle_close(
                            sym, pos, mark, pnl, "TRAIL", args, recovery, hedge, filt, api, sec,
                            adaptive_state=adaptive,
                        )
                        pos = None
                elif not use_ob_exits and pnl >= args.tp_pct and not should_tp_close(
                    pnl, args.tp_pct, args.fee_buffer,
                ):
                    print(
                        f"{DIM}TP gross {pnl:+.3f}% but est. net "
                        f"{pnl - args.fee_buffer:+.3f}% ≤ 0 — holding "
                        f"{_side_label(pos.is_long)}{RESET}",
                    )
                elif sl_hit:
                    _print_close_event("SL", pos, pnl, mark, fee_buffer=args.fee_buffer)
                    last_close_at = _handle_close(
                        sym, pos, mark, pnl, "SL", args, recovery, hedge, filt, api, sec,
                        adaptive_state=adaptive,
                    )
                    pos = None

            bar = builder.add_sample(bids, asks, now)
            if bar is None:
                time.sleep(args.sample_sec)
                continue

            ob_signal = entry_signal(bar, sig_cfg)
            # Always gather context for multi-trigger (and optional AND filters).
            ema_snap = None
            try:
                ema_snap = fetch_ema_snapshot(
                    sym,
                    interval=args.ema_interval,
                    fast=args.ema_fast,
                    slow=args.ema_slow,
                    slope_bars=args.ema_slope_bars,
                    slope_min_pct=ema_slope,
                )
            except Exception as exc:
                print(f"{YELLOW}EMA fetch failed: {exc}{RESET}")

            bar_rec = BarRecord.from_bar(bar, ob_signal=ob_signal, ema=ema_snap)
            print_bar(bar, ob_signal)
            append_bar(sym, bar_rec)
            if ema_snap:
                print(format_ema_console(ema_snap))
                extra = f" ob={ob_signal or 'none'}" if ob_signal else ""
                append_ema_log(sym, ema_snap.log_line() + extra)

            pat_snap = None
            if args.pattern_filter or getattr(args, "multi_trigger", True):
                try:
                    pat_cfg = PatternConfig(
                        interval=args.pattern_interval,
                        fvg_interval=args.pattern_fvg_interval,
                        min_body_ratio=args.pattern_min_body_ratio,
                        min_continuity=args.pattern_min_continuity,
                        min_vol_ratio=args.pattern_min_vol_ratio,
                    )
                    pat_snap = evaluate_pattern(sym, cfg=pat_cfg)
                except Exception as exc:
                    print(f"{YELLOW}Pattern fetch failed: {exc}{RESET}")
                if pat_snap:
                    print(format_pattern_console(pat_snap))

            struct_snap = None
            if getattr(args, "multi_trigger", True) and (
                _env_trig("OB_TRIG_CHOCH", True)
                or _env_trig("OB_TRIG_EQL", True)
                or _env_trig("OB_TRIG_EQH", True)
            ):
                try:
                    struct_cfg = StructureConfig(
                        interval=getattr(args, "structure_interval", "5m"),
                        equal_tol_pct=float(getattr(args, "structure_equal_tol", 0.12)),
                        near_pct=float(getattr(args, "structure_near_pct", 0.35)),
                    )
                    struct_snap = fetch_structure(sym, cfg=struct_cfg)
                    print(format_structure_console(struct_snap))
                except Exception as exc:
                    print(f"{YELLOW}Structure fetch failed: {exc}{RESET}")

            osc_snap = None
            if getattr(args, "multi_trigger", True) and (
                _env_trig("OB_TRIG_RSI", True) or _env_trig("OB_TRIG_STOCH", True)
            ):
                try:
                    osc_cfg = OscillatorConfig(
                        interval=getattr(args, "osc_interval", "5m"),
                        rsi_period=int(getattr(args, "rsi_period", 14)),
                        rsi_oversold=float(getattr(args, "rsi_oversold", 30.0)),
                        rsi_overbought=float(getattr(args, "rsi_overbought", 70.0)),
                        stoch_k=int(getattr(args, "stoch_k", 14)),
                        stoch_d=int(getattr(args, "stoch_d", 3)),
                        stoch_oversold=float(getattr(args, "stoch_oversold", 20.0)),
                        stoch_overbought=float(getattr(args, "stoch_overbought", 80.0)),
                    )
                    osc_snap = fetch_oscillators(sym, cfg=osc_cfg)
                    print(format_oscillators_console(osc_snap))
                except Exception as exc:
                    print(f"{YELLOW}Oscillator fetch failed: {exc}{RESET}")

            ml_prob_long = ml_prob_short = None
            if ml_model and args.ml_filter:
                ml_prob_long = predict_prob(ml_model, bar_rec, "long")
                ml_prob_short = predict_prob(ml_model, bar_rec, "short")

            trigger_tag = ""
            if getattr(args, "multi_trigger", True):
                enable = {
                    "momentum": _env_trig("OB_TRIG_MOMENTUM", True),
                    "imbalance": _env_trig("OB_TRIG_IMBALANCE", True),
                    "ema_trend": _env_trig("OB_TRIG_EMA_TREND", True),
                    "ema_cross": _env_trig("OB_TRIG_EMA_CROSS", True),
                    "htf": (
                        _env_trig("OB_TRIG_HTF", True)
                        if os.getenv("OB_TRIG_HTF", "").strip()
                        else _env_trig("OB_TRIG_PATTERN", True)
                    ) and bool(args.pattern_filter or True),
                    "candles": _env_trig("OB_TRIG_CANDLES", True),
                    "ml": _env_trig("OB_TRIG_ML", True) and bool(args.ml_filter and ml_model),
                    "choch": _env_trig("OB_TRIG_CHOCH", True),
                    "eql": _env_trig("OB_TRIG_EQL", True),
                    "eqh": _env_trig("OB_TRIG_EQH", True),
                    "rsi": _env_trig("OB_TRIG_RSI", True),
                    "stoch": _env_trig("OB_TRIG_STOCH", True),
                }
                decision = collect_triggers(
                    bar, sig_cfg,
                    ema=ema_snap,
                    pattern=pat_snap,
                    structure=struct_snap,
                    oscillators=osc_snap,
                    ml_prob_long=ml_prob_long,
                    ml_prob_short=ml_prob_short,
                    ml_min_prob=ml_threshold,
                    min_hits=int(getattr(args, "trig_min_hits", 2) or 1),
                    enable=enable,
                )
                signal = decision.side
                trigger_tag = decision.tag
                if signal and trigger_tag:
                    print(f"{CYAN}Triggers {signal.upper()}: {trigger_tag}{RESET}")
                elif not signal and getattr(args, "trig_min_hits", 2) > 1:
                    # Quiet unless we almost had a signal (avoid spam)
                    pass
            else:
                signal = ob_signal
                trigger_tag = "momentum" if signal and not args.imb_filter else ("imbalance" if signal else "")
                if signal and args.ema_filter and ema_snap and not ema_allows(signal, ema_snap):
                    print(
                        f"{YELLOW}EMA filter block {signal.upper()} "
                        f"(trend={ema_snap.trend}, slope={ema_snap.slope_pct:+.3f}%){RESET}",
                    )
                    append_ema_log(sym, f"BLOCK {signal.upper()} trend={ema_snap.trend}")
                    signal = None
                if signal and args.pattern_filter and pat_snap and not pattern_allows(signal, pat_snap):
                    print(
                        f"{YELLOW}Pattern filter block {signal.upper()} "
                        f"({pat_snap.reason}){RESET}",
                    )
                    signal = None
                if signal and ml_model and args.ml_filter:
                    prob = predict_prob(ml_model, bar_rec, signal)
                    if prob < ml_threshold:
                        print(
                            f"{YELLOW}ML filter block {signal.upper()} "
                            f"(prob={prob:.2f} < {ml_threshold:.2f}){RESET}",
                        )
                        signal = None

            if pos is not None:
                _print_open_position(pos, bar.mid_c, args)

            _print_pnl_summary(sym, pos, bar.mid_c if pos else mark)

            if pos is not None:
                pos.bars_held += 1
                if exit_on_flip(pos.is_long, bar, sig_cfg):
                    pnl = profit_pct(pos.entry, bar.mid_c, pos.is_long)
                    if should_discretionary_close(pnl, args.fee_buffer):
                        ok, hold_msg = _recovery_allows_soft_exit(
                            recover=args.recover,
                            recovery=recovery,
                            entry=pos.entry,
                            exit_price=bar.mid_c,
                            qty=pos.qty,
                            is_long=pos.is_long,
                            fee_buffer=args.fee_buffer,
                        )
                        if not ok:
                            print(f"{DIM}Flip signal but {hold_msg}{RESET}")
                        else:
                            _print_close_event("FLIP", pos, pnl, bar.mid_c, fee_buffer=args.fee_buffer)
                            last_close_at = _handle_close(
                                sym, pos, bar.mid_c, pnl, "FLIP", args, recovery, hedge, filt, api, sec,
                                adaptive_state=adaptive,
                            )
                            pos = None
                    else:
                        print(
                            f"{DIM}Flip signal but est. net {pnl - args.fee_buffer:+.3f}% ≤ 0 — holding{RESET}",
                        )
                elif pos.bars_held >= args.max_bars:
                    pnl = profit_pct(pos.entry, bar.mid_c, pos.is_long)
                    if should_discretionary_close(pnl, args.fee_buffer):
                        ok, hold_msg = _recovery_allows_soft_exit(
                            recover=args.recover,
                            recovery=recovery,
                            entry=pos.entry,
                            exit_price=bar.mid_c,
                            qty=pos.qty,
                            is_long=pos.is_long,
                            fee_buffer=args.fee_buffer,
                        )
                        if not ok:
                            print(f"{DIM}Max bars but {hold_msg}{RESET}")
                        else:
                            _print_close_event("MAXBARS", pos, pnl, bar.mid_c, fee_buffer=args.fee_buffer)
                            last_close_at = _handle_close(
                                sym, pos, bar.mid_c, pnl, "MAXBARS", args, recovery, hedge, filt, api, sec,
                                adaptive_state=adaptive,
                            )
                            pos = None
                    else:
                        print(
                            f"{DIM}Max bars but est. net {pnl - args.fee_buffer:+.3f}% ≤ 0 — holding{RESET}",
                        )

            elif signal and not args.dry_run:
                if is_draining(sym):
                    print(
                        f"{DIM}Drain mode — skip {signal.upper()} entry "
                        f"(waiting for TP/SL flat){RESET}",
                    )
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue
                is_long = signal == "long"
                if last_close_at and (now - last_close_at) < args.entry_cooldown_sec:
                    wait = args.entry_cooldown_sec - (now - last_close_at)
                    print(
                        f"{DIM}Entry cooldown {wait:.0f}s — skip {signal.upper()} "
                        f"(wait after last close){RESET}",
                    )
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue
                if args.recover and recovery.locked_side:
                    if maybe_expire_side_lock(
                        sym, recovery, lock_min=args.recover_lock_min, dry_run=args.dry_run,
                    ):
                        print(
                            f"{YELLOW}Recovery side-lock expired ({args.recover_lock_min:g}m) — "
                            f"both sides allowed (size still {recovery.multiplier:g}x){RESET}",
                        )
                if args.recover and recovery.locked_side and signal != recovery.locked_side:
                    print(
                        f"{YELLOW}Recovery locked {recovery.locked_side.upper()} — "
                        f"skip {signal.upper()} (same side ≤{args.recover_lock_min:g}m){RESET}",
                    )
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue
                opp_open, opp_qty = _opposite_position_open(
                    sym, is_long, hedge, api, sec, args.recv_window,
                )
                if opp_open:
                    print(
                        f"{RED}Opposite hedge leg open ({opp_qty:g}) — "
                        f"skip {signal.upper()} until flat.{RESET}",
                    )
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue
                if args.recover and recovery.level > args.recover_max_level:
                    print(
                        f"{RED}Recovery max level {args.recover_max_level} exceeded — "
                        f"skipping entry (reset with --reset-recover).{RESET}",
                    )
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue
                try:
                    qty_str, qty_f, notional = resolve_entry_qty(
                        args, bar.mid_c, filt, api, sec, recovery=recovery if args.recover else None,
                    )
                except ValueError as exc:
                    print(f"{RED}Sizing failed: {exc}{RESET}")
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue

                if args.recover:
                    save_state(sym, recovery)

                cid = client_id(sym, "E")
                direction = "LONG" if is_long else "SHORT"
                side_color = _side_color("long" if is_long else "short")
                mult_note = f" · {recovery.multiplier:g}x recover" if args.recover and recovery.level > 0 else ""
                trig_note = f" · via {trigger_tag}" if trigger_tag else ""

                exits = None
                if getattr(args, "ob_exits", True):
                    try:
                        exits = resolve_ob_exits(
                            bids, asks, bar.mid_c, is_long,
                            fee_buffer=args.fee_buffer,
                            tick=filt["tick_size"],
                            tp_pct_fallback=args.tp_pct,
                            sl_pct_fallback=sl_threshold,
                            wall_min_mult=float(getattr(args, "ob_wall_min_mult", 1.0)),
                            max_range_pct=float(getattr(args, "ob_exit_max_pct", 3.0)),
                        )
                    except Exception as exc:
                        print(f"{YELLOW}OB exits resolve failed ({exc}) — using % TP/SL{RESET}")

                print(
                    f"{BOLD}{side_color}▶ MARKET {direction} {qty_str} @ ~{price_fmt(bar.mid_c)} "
                    f"(imb {bar.imbalance * 100:.1f}%{mult_note}{trig_note}){RESET}",
                )
                if exits:
                    print(f"  {DIM}exits OB · {exits.note}  TP {price_fmt(exits.tp_price)}  SL {price_fmt(exits.sl_price)}{RESET}")
                try:
                    market_open(sym, is_long, qty_str, hedge, api, sec, args.recv_window, cid=cid)
                    pos = PositionState(
                        is_long=is_long,
                        entry=bar.mid_c,
                        qty=qty_f,
                        opened_at=now,
                        recovery_level=recovery.level if args.recover else 0,
                        entry_features=feature_vector(bar_rec),
                        entry_signal=signal,
                        trigger=trigger_tag,
                        tp_price=exits.tp_price if exits else 0.0,
                        sl_price=exits.sl_price if exits else 0.0,
                        exits_note=exits.note if exits else "",
                    )
                    if pos.tp_price <= 0 or pos.sl_price <= 0:
                        pos.tp_price, pos.sl_price = _tp_sl_prices(
                            pos.entry, pos.is_long, args.tp_pct, sl_threshold,
                        )
                    _arm_exchange_exits(sym, pos, bar.mid_c, args, hedge, filt, api, sec, force=True)
                    if args.adaptive and adaptive is not None:
                        on_trade_open(sym, adaptive)
                    trig_j = f" trigger={trigger_tag}" if trigger_tag else ""
                    append_journal(
                        sym,
                        f"OPEN {direction} qty={qty_f:g} notional={notional:.4f} "
                        f"level={recovery.level if args.recover else 0} "
                        f"({recovery.multiplier if args.recover else 1:g}x){trig_j}",
                    )
                    play_sound("entry")
                except RuntimeError as exc:
                    print(f"{RED}Market entry failed: {exc}{RESET}")

            elif signal and args.dry_run:
                if is_draining(sym):
                    print(f"{DIM}Drain mode — skip dry-run {signal.upper()}{RESET}")
                else:
                    direction = signal.upper()
                    side_color = _side_color(signal)
                    mult_note = ""
                    if args.recover:
                        try:
                            _, _, base_notional = resolve_entry_qty(
                                args, bar.mid_c, filt, api, sec, recovery=None,
                            )
                            next_notional, next_level = target_notional(
                                recovery, base_notional, max_level=args.recover_max_level,
                            )
                            mult = 2 ** next_level
                            mult_note = f" · would {mult:g}x ({next_notional:.2f} USDT)"
                        except ValueError:
                            pass
                    trig_note = f" · via {trigger_tag}" if trigger_tag else ""
                    print(
                        f"{side_color}  [dry-run] would MARKET {direction} "
                        f"@ ~{price_fmt(bar.mid_c)}{mult_note}{trig_note}{RESET}",
                    )

            builder.reset_after_bar(now)
            time.sleep(max(0.0, args.sample_sec))

    except KeyboardInterrupt:
        print(f"\n{RESET}Stopped OB scalp (position left as-is on Binance).")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OB scalp: synthetic bars from depth, market entries (separate from DCA grid).",
    )
    p.add_argument("symbol", help="Futures symbol, e.g. BTCUSDT")
    p.add_argument("--bar-sec", type=float, default=_env_float("OB_BAR_SEC", 60.0),
                   help="Internal bar length in seconds (default: 60)")
    p.add_argument("--sample-sec", type=float, default=_env_float("OB_SAMPLE_SEC", 2.0),
                   help="Depth poll interval while building a bar (default: 2)")
    p.add_argument("--band-pct", type=float, default=_env_float("OB_BAND_PCT", 1.0),
                   help="Book band around mid for imbalance (default: 1%%)")
    p.add_argument("--limit", type=int, default=100, help="Depth levels to fetch")
    p.add_argument("--imb-long", type=float, default=_env_float("OB_IMB_LONG", 0.55),
                   help="Enter long when imbalance >= this (default: 0.55)")
    p.add_argument("--imb-short", type=float, default=_env_float("OB_IMB_SHORT", 0.45),
                   help="Enter short when imbalance <= this (default: 0.45)")
    p.add_argument("--imb-filter", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_IMB_FILTER", False),
                   help="Use book imbalance for entry/flip (default off — scalp uses momentum+EMA+SL)")
    p.add_argument("--min-wall-qty", type=float, default=_env_float("OB_MIN_WALL_QTY", 0.0),
                   help="Min resting size on signal-side wall (0=off)")
    p.add_argument("--no-momentum", action="store_true",
                   help="Do not require mid move in signal direction")
    p.add_argument("--momentum-min-pct", type=float, default=_env_float("OB_MOMENTUM_MIN_PCT", 0.01),
                   help="Min mid change %% in bar for entry (default: 0.01)")
    p.add_argument("--tp-pct", type=float, default=_env_float("OB_TP_PCT", 0.25),
                   help="Take profit %% (default: 0.25)")
    p.add_argument("--sl-pct", type=float, default=_env_float("OB_SL_PCT", 0.15),
                   help="Stop loss %% (default: 0.15)")
    p.add_argument("--trail-pct", type=float, default=_env_float("OB_TRAIL_PCT", 0.12),
                   help="Trailing stop distance %% from peak/trough once armed (0=off, default 0.12)")
    p.add_argument("--trail-arm-pct", type=float, default=_env_float("OB_TRAIL_ARM_PCT", 0.0),
                   help="Arm trailing after this gross profit %% (default: ~65%% of TP or fee+0.15)")
    p.add_argument("--fee-buffer", type=float, default=_env_float("OB_FEE_BUFFER", 0.12),
                   help="Round-trip fee+slippage estimate %%; TP/flip/trail skip close if gross-fee ≤ 0")
    p.add_argument("--max-bars", type=int, default=int(_env_float("OB_MAX_BARS", 12)),
                   help="Max bars to hold before time exit (default: 12 — follow moves longer)")
    p.add_argument("--pattern-filter", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_PATTERN_FILTER", True),
                   help="Require HTF candle continuity/volume + ATR/BB/FVG context (default on)")
    p.add_argument("--pattern-interval", default=os.getenv("OB_PATTERN_INTERVAL", "5m").strip() or "5m",
                   help="Kline interval for continuity/volume (default 5m)")
    p.add_argument("--pattern-fvg-interval", default=os.getenv("OB_PATTERN_FVG_INTERVAL", "15m").strip() or "15m",
                   help="Kline interval for FVG scan (default 15m)")
    p.add_argument("--pattern-min-body-ratio", type=float,
                   default=_env_float("OB_PATTERN_MIN_BODY_RATIO", 0.40),
                   help="Min candle body/range for a real candle (default 0.40)")
    p.add_argument("--pattern-min-continuity", type=int,
                   default=int(_env_float("OB_PATTERN_MIN_CONTINUITY", 1)),
                   help="Min consecutive same-direction HTF candles (default 1)")
    p.add_argument("--pattern-min-vol-ratio", type=float,
                   default=_env_float("OB_PATTERN_MIN_VOL_RATIO", 0.90),
                   help="Min last-candle volume vs 20-bar avg (default 0.90)")
    p.add_argument("--multi-trigger", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_MULTI_TRIGGER", True),
                   help="OR entry triggers (momentum/imb/ema/pattern/ml) and tag trades (default on)")
    p.add_argument("--trig-min-hits", type=int,
                   default=int(_env_float("OB_TRIG_MIN_HITS", 2)),
                   help="Min agreeing triggers for entry when --multi-trigger (default 2)")
    p.add_argument("--structure-interval", default=os.getenv("OB_STRUCTURE_INTERVAL", "5m").strip() or "5m",
                   help="Kline interval for choch/eql/eqh (default 5m)")
    p.add_argument("--structure-equal-tol", type=float,
                   default=_env_float("OB_STRUCTURE_EQUAL_TOL", 0.12),
                   help="EQH/EQL match tolerance %% (default 0.12)")
    p.add_argument("--osc-interval", default=os.getenv("OB_OSC_INTERVAL", "5m").strip() or "5m",
                   help="Kline interval for RSI/Stochastic (default 5m)")
    p.add_argument("--rsi-period", type=int, default=int(_env_float("OB_RSI_PERIOD", 14)),
                   help="RSI period (default 14)")
    p.add_argument("--rsi-oversold", type=float, default=_env_float("OB_RSI_OVERSOLD", 30.0),
                   help="RSI long zone (default 30)")
    p.add_argument("--rsi-overbought", type=float, default=_env_float("OB_RSI_OVERBOUGHT", 70.0),
                   help="RSI short zone (default 70)")
    p.add_argument("--stoch-k", type=int, default=int(_env_float("OB_STOCH_K", 14)),
                   help="Stochastic %%K period (default 14)")
    p.add_argument("--stoch-d", type=int, default=int(_env_float("OB_STOCH_D", 3)),
                   help="Stochastic %%D smooth (default 3)")
    p.add_argument("--stoch-oversold", type=float, default=_env_float("OB_STOCH_OVERSOLD", 20.0),
                   help="Stochastic long zone (default 20)")
    p.add_argument("--stoch-overbought", type=float, default=_env_float("OB_STOCH_OVERBOUGHT", 80.0),
                   help="Stochastic short zone (default 80)")
    p.add_argument("--structure-near-pct", type=float,
                   default=_env_float("OB_STRUCTURE_NEAR_PCT", 0.35),
                   help="Max distance %% to EQH/EQL to fire trigger (default 0.35)")
    p.add_argument("--ob-exits", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_EXITS", True),
                   help="TP/SL from order-book walls with %% fallback (default on)")
    p.add_argument("--exchange-exits", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_EXCHANGE_EXITS", True),
                   help="Place TAKE_PROFIT_MARKET + STOP_MARKET on Binance at open (default on)")
    p.add_argument("--ob-wall-min-mult", type=float, default=_env_float("OB_WALL_MIN_MULT", 1.0),
                   help="Min wall size vs median book qty for OB TP (default 1.0)")
    p.add_argument("--ob-exit-max-pct", type=float, default=_env_float("OB_EXIT_MAX_PCT", 3.0),
                   help="Max wall distance %% before falling back to %% TP/SL (default 3)")
    p.add_argument(
        "--size-mode",
        choices=["min", "wallet", "fixed"],
        default=os.getenv("SCALP_SIZE_MODE", "min").strip().lower() or "min",
        help="min=exchange min qty (default), wallet=%% wallet, fixed=--base-size USDT",
    )
    p.add_argument("--wallet-pct", type=float, default=_env_float("SCALP_WALLET_PCT", _env_float("WALLET_PCT", 2.0)),
                   help="With --size-mode wallet: entry notional as %% of wallet")
    p.add_argument("--base-size", type=float, default=_env_float("SCALP_BASE_SIZE", 0.0),
                   help="With --size-mode fixed: USDT notional per trade")
    p.add_argument("--recover", action="store_true", default=_env_bool("OB_RECOVER", False),
                   help="After a losing close, double size on SAME side until TP (state in .run/logs/SYMBOL/)")
    p.add_argument("--recover-max-level", type=int, default=int(_env_float("OB_RECOVER_MAX_LEVEL", 4)),
                   help="Max martingale level (2^level multiplier cap, default 4 = 16x)")
    p.add_argument("--recover-lock-min", type=float, default=_env_float("OB_RECOVER_LOCK_MIN", 5.0),
                   help="Minutes to keep same-side lock after loss (0=until debt cleared, default 5)")
    p.add_argument("--entry-cooldown-sec", type=float, default=_env_float("OB_ENTRY_COOLDOWN_SEC", 45.0),
                   help="Seconds to wait after a close before a new entry (default 45)")
    p.add_argument("--ema-filter", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_EMA_FILTER", True),
                   help="Require 1m EMA trend alignment for entries (default on)")
    p.add_argument("--ema-interval", default=os.getenv("OB_EMA_INTERVAL", "1m").strip() or "1m",
                   help="Kline interval for EMA filter (default 1m)")
    p.add_argument("--ema-fast", type=int, default=int(_env_float("OB_EMA_FAST", 7)),
                   help="Fast EMA period (default 7)")
    p.add_argument("--ema-slow", type=int, default=int(_env_float("OB_EMA_SLOW", 25)),
                   help="Slow EMA period (default 25)")
    p.add_argument("--ema-slope-bars", type=int, default=int(_env_float("OB_EMA_SLOPE_BARS", 5)),
                   help="Bars to measure fast EMA slope (default 5)")
    p.add_argument("--ema-slope-min", type=float, default=_env_float("OB_EMA_SLOPE_MIN", 0.05),
                   help="Min fast EMA slope %% for trend (default 0.05)")
    p.add_argument("--ml-filter", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_ML_FILTER", True),
                   help="Use RandomForest model from autotune if present (default on)")
    p.add_argument("--ml-min-prob", type=float, default=_env_float("OB_ML_MIN_PROB", 0.48),
                   help="Min ML win probability to enter (default 0.48)")
    p.add_argument("--adaptive", action=argparse.BooleanOptionalAction,
                   default=_env_bool("OB_ADAPTIVE", True),
                   help="Permissive adaptive filters + learn from trade outcomes (default on)")
    p.add_argument("--reset-recover", action="store_true",
                   help="Reset recovery state to level 0 before starting")
    p.add_argument("--dry-run", action="store_true", help="Log signals only, no orders")
    p.add_argument("--execute", action="store_true", help="Send market orders (required for live)")
    p.add_argument("--force", action="store_true", help="Run even if DCA supervisor is active on symbol")
    p.add_argument("--no-sounds", action="store_true", help="Disable local trade sounds (OB_SOUNDS=0)")
    p.add_argument("--position-mode", choices=["auto", "hedge", "oneway"], default="auto")
    p.add_argument("--recv-window", type=int, default=int(_env_float("RECV_WINDOW", 15000)))
    p.add_argument("--env-file", default=None)
    return p.parse_args()


def main() -> None:
    load_env_file(None)
    args = parse_args()
    args.symbol = args.symbol.upper()
    if args.trail_arm_pct <= 0 and args.trail_pct > 0:
        # Arm later so trail does not cut winners before fees + room to run
        args.trail_arm_pct = max(args.tp_pct * 0.65, args.fee_buffer + 0.15)
    if args.no_sounds:
        os.environ["OB_SOUNDS"] = "0"

    if not args.dry_run and not args.execute:
        print(
            f"{YELLOW}Pass --execute for live orders or --dry-run to observe signals.{RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    run_loop(args)


if __name__ == "__main__":
    main()
