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
from typing import Any

from ob_bars import BarBuilder, depth_to_levels
from ob_signals import SignalConfig, entry_signal, exit_on_flip, profit_pct

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


def resolve_entry_qty(
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


def print_bar(bar: Any, signal: str | None) -> None:
    sig = f"  {GREEN}→ {signal.upper()}{RESET}" if signal else ""
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
    bars_held: int = 0


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
    sig_cfg = SignalConfig(
        imb_long=args.imb_long,
        imb_short=args.imb_short,
        min_wall_qty=args.min_wall_qty,
        require_momentum=not args.no_momentum,
        momentum_min_pct=args.momentum_min_pct,
    )
    builder = BarBuilder(bar_sec=args.bar_sec, band_pct=args.band_pct)

    try:
        _preview_bids, _preview_asks = depth_to_levels(fetch_depth(sym, args.limit))
        _preview_mid = (_preview_bids[0][0] + _preview_asks[0][0]) / 2 if _preview_bids and _preview_asks else 0.0
    except Exception:
        _preview_mid = 0.0
    size_note = size_mode_summary(args, filt, _preview_mid) if _preview_mid > 0 else args.size_mode

    mode = f"{YELLOW}DRY-RUN{RESET}" if args.dry_run else f"{GREEN}LIVE{RESET}"
    print(
        f"\n{BOLD}{CYAN}OB scalp · {sym}{RESET}  {mode}\n"
        f"  bar {args.bar_sec:g}s  sample {args.sample_sec:g}s  band ±{args.band_pct:g}%\n"
        f"  entry imb long≥{args.imb_long:.2f} short≤{args.imb_short:.2f}  "
        f"TP +{args.tp_pct:g}%  SL -{args.sl_pct:g}%  max {args.max_bars} bars\n"
        f"  size {size_note}  Ctrl+C to stop\n",
    )

    pos: PositionState | None = None
    builder.start_bar(time.time())

    try:
        while True:
            now = time.time()
            try:
                depth = fetch_depth(sym, args.limit)
                bids, asks = depth_to_levels(depth)
            except Exception as exc:
                print(f"{RED}Depth fetch failed: {exc}{RESET}")
                time.sleep(args.sample_sec)
                continue

            if not bids or not asks:
                time.sleep(args.sample_sec)
                continue

            mark = (bids[0][0] + asks[0][0]) / 2

            side, live_qty, live_entry = _detect_open_side(sym, hedge, api, sec, args.recv_window)
            if side is not None and live_qty > 0:
                if pos is None:
                    pos = PositionState(
                        is_long=side,
                        entry=live_entry,
                        qty=live_qty,
                        opened_at=now,
                    )
                else:
                    pos.qty = live_qty
                    pos.entry = live_entry
                    pos.is_long = side
            elif pos is not None and live_qty <= 0:
                print(f"{DIM}Position flat on exchange — clearing local state.{RESET}")
                pos = None

            if pos is not None:
                pnl = profit_pct(pos.entry, mark, pos.is_long)
                if pnl >= args.tp_pct:
                    print(f"{GREEN}TP hit {pnl:+.3f}% @ {price_fmt(mark)}{RESET}")
                    if not args.dry_run:
                        market_close_position(sym, pos.is_long, pos.qty, hedge, filt, api, sec, args.recv_window)
                    pos = None
                elif pnl <= -args.sl_pct:
                    print(f"{RED}SL hit {pnl:+.3f}% @ {price_fmt(mark)}{RESET}")
                    if not args.dry_run:
                        market_close_position(sym, pos.is_long, pos.qty, hedge, filt, api, sec, args.recv_window)
                    pos = None

            bar = builder.add_sample(bids, asks, now)
            if bar is None:
                time.sleep(args.sample_sec)
                continue

            signal = entry_signal(bar, sig_cfg)
            print_bar(bar, signal)

            if pos is not None:
                pos.bars_held += 1
                if exit_on_flip(pos.is_long, bar, sig_cfg):
                    pnl = profit_pct(pos.entry, bar.mid_c, pos.is_long)
                    print(f"{YELLOW}Imbalance flip → exit {pnl:+.3f}%{RESET}")
                    if not args.dry_run:
                        market_close_position(sym, pos.is_long, pos.qty, hedge, filt, api, sec, args.recv_window)
                    pos = None
                elif pos.bars_held >= args.max_bars:
                    pnl = profit_pct(pos.entry, bar.mid_c, pos.is_long)
                    print(f"{YELLOW}Max bars ({args.max_bars}) → exit {pnl:+.3f}%{RESET}")
                    if not args.dry_run:
                        market_close_position(sym, pos.is_long, pos.qty, hedge, filt, api, sec, args.recv_window)
                    pos = None

            elif signal and not args.dry_run:
                is_long = signal == "long"
                try:
                    qty_str, qty_f = resolve_entry_qty(args, bar.mid_c, filt, api, sec)
                except ValueError as exc:
                    print(f"{RED}Sizing failed: {exc}{RESET}")
                    builder.reset_after_bar(now)
                    time.sleep(args.sample_sec)
                    continue

                cid = client_id(sym, "E")
                direction = "LONG" if is_long else "SHORT"
                print(
                    f"{BOLD}{GREEN}▶ MARKET {direction} {qty_str} @ ~{price_fmt(bar.mid_c)} "
                    f"(imb {bar.imbalance * 100:.1f}%){RESET}",
                )
                try:
                    market_open(sym, is_long, qty_str, hedge, api, sec, args.recv_window, cid=cid)
                    pos = PositionState(is_long=is_long, entry=bar.mid_c, qty=qty_f, opened_at=now)
                except RuntimeError as exc:
                    print(f"{RED}Market entry failed: {exc}{RESET}")

            elif signal and args.dry_run:
                direction = signal.upper()
                print(f"{CYAN}  [dry-run] would MARKET {direction} @ ~{price_fmt(bar.mid_c)}{RESET}")

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
    p.add_argument("--max-bars", type=int, default=int(_env_float("OB_MAX_BARS", 5)),
                   help="Max bars to hold before time exit (default: 5)")
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
    p.add_argument("--dry-run", action="store_true", help="Log signals only, no orders")
    p.add_argument("--execute", action="store_true", help="Send market orders (required for live)")
    p.add_argument("--force", action="store_true", help="Run even if DCA supervisor is active on symbol")
    p.add_argument("--position-mode", choices=["auto", "hedge", "oneway"], default="auto")
    p.add_argument("--recv-window", type=int, default=int(_env_float("RECV_WINDOW", 15000)))
    p.add_argument("--env-file", default=None)
    return p.parse_args()


def main() -> None:
    load_env_file(None)
    args = parse_args()
    args.symbol = args.symbol.upper()

    if not args.dry_run and not args.execute:
        print(
            f"{YELLOW}Pass --execute for live orders or --dry-run to observe signals.{RESET}",
            file=sys.stderr,
        )
        sys.exit(1)

    run_loop(args)


if __name__ == "__main__":
    main()
