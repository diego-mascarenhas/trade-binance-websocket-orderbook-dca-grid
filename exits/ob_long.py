"""Exit: soft-close when the order book flips against us.

SHORT → close on OB Long (imbalance ≥ imb_long)
LONG  → close on OB Short (imbalance ≤ imb_short)

Independent of BE — use ``--protect-be`` separately if you also want a BE SL.
Uses a single depth snapshot per supervise poll (same imbalance metric as scalp).
No exchange TAKE_PROFIT — market-closes like structure TP.
"""

from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal

from ob_bars import OBBar, _book_metrics, depth_to_levels
from ob_signals import SignalConfig, exit_on_flip, profit_pct

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


def signal_config_from_args(args: argparse.Namespace) -> SignalConfig:
    imb_long = getattr(args, "imb_long", None)
    imb_short = getattr(args, "imb_short", None)
    band = getattr(args, "ob_band_pct", None)
    return SignalConfig(
        imb_long=float(imb_long if imb_long is not None else _env_float("IMB_LONG", 0.55)),
        imb_short=float(imb_short if imb_short is not None else _env_float("IMB_SHORT", 0.45)),
        require_momentum=False,
        use_imbalance=True,
    )


def _band_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "ob_band_pct", None)
    if v is not None:
        return float(v)
    return _env_float("OB_BAND_PCT", 1.0)


def _snapshot_bar(bids: list[list[float]], asks: list[list[float]], band_pct: float) -> OBBar | None:
    m = _book_metrics(bids, asks, band_pct=band_pct)
    mid = float(m["mid"] or 0)
    if mid <= 0:
        return None
    now = time.time()
    return OBBar(
        t_open=now,
        t_close=now,
        mid_o=mid,
        mid_h=mid,
        mid_l=mid,
        mid_c=mid,
        spread_avg=float(m["spread"] or 0),
        imbalance=float(m["imbalance"] or 0.5),
        bid_vol=float(m["bid_vol"] or 0),
        ask_vol=float(m["ask_vol"] or 0),
        bid_wall_price=float(m["bid_wall_price"] or 0),
        bid_wall_qty=float(m["bid_wall_qty"] or 0),
        ask_wall_price=float(m["ask_wall_price"] or 0),
        ask_wall_qty=float(m["ask_wall_qty"] or 0),
        samples=1,
    )


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
    from exits.structure import cancel_close_algos

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    side = "LONG" if side_is_long else "SHORT"

    if entry <= 0 or qty <= 0:
        return

    cfg = signal_config_from_args(args)
    band = _band_pct(args)
    try:
        depth = grid.fetch_depth(symbol, getattr(args, "limit", 50))
        bids, asks = depth_to_levels(depth)
    except Exception as exc:
        print(f"{grid.YELLOW}OB-flip depth skip: {exc}{grid.RESET}")
        return

    bar = _snapshot_bar(bids, asks, band)
    if bar is None:
        return

    need = "OB Long" if not side_is_long else "OB Short"
    if not side_is_long:
        need_note = f"imb≥{cfg.imb_long:g}"
    else:
        need_note = f"imb≤{cfg.imb_short:g}"
    if not exit_on_flip(side_is_long, bar, cfg):
        print(
            f"{grid.DIM}OB-flip armed · {side} imb={bar.imbalance:.3f} "
            f"(need {need} {need_note}) · "
            f"pnl={profit_pct(entry, bar.mid_c, side_is_long):+.3f}%{grid.RESET}"
        )
        return

    reason = f"{need} imb={bar.imbalance:.3f}"
    print(
        f"{grid.GREEN}✓ OB-flip TP {side} · {reason} · "
        f"pnl={profit_pct(entry, bar.mid_c, side_is_long):+.3f}% "
        f"@ {grid.price_fmt(bar.mid_c)}{grid.RESET}"
    )
    if bool(getattr(args, "dry_run", False)):
        _CLOSE_REASONS[symbol.upper()] = f"OB-flip · {reason}"
        return

    try:
        try:
            grid.cancel_all_symbol_orders(symbol, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel open orders: {exc}{grid.RESET}")
        cancel_close_algos(symbol, side_is_long, api, sec, recv)
        closed = grid.market_close_position(
            symbol, side_is_long, qty, hedge, filt, api, sec, recv,
        )
        print(f"{grid.GREEN}✓ Market-closed {closed:g} ({reason}){grid.RESET}")
        _CLOSE_REASONS[symbol.upper()] = f"OB-flip · {reason}"
        time.sleep(0.35)
    except Exception as exc:
        print(f"{grid.RED}✗ OB-flip close failed: {exc}{grid.RESET}")
