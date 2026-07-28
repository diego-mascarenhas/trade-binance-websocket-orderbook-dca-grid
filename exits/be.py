"""Exit: protect-BE only (no TP1, no trail).

When unrealized profit ≥ --be-arm-pct (default 1%), place a reduce-only
STOP_MARKET on the full position at entry ± --be-profit-pct (default 0.3%).
Lock is pure profit % from entry — no fee buffer.

Used alone via `--exit be`, or as the protect layer under `--exit structure`
(TP remains EQH/EQL). DCA grid stays active; BE qty is resynced if size grows.
"""

from __future__ import annotations

import argparse
import os
from decimal import Decimal


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def be_arm_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "be_arm_pct", None)
    if v is not None:
        return float(v)
    return _env_float("BE_ARM_PCT", 1.0)


def be_profit_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "be_profit_pct", None)
    if v is not None:
        return float(v)
    return _env_float("BE_PROFIT_PCT", 0.3)


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
    arm_pct = be_arm_pct(args)
    lock_pct = be_profit_pct(args)
    side = "LONG" if side_is_long else "SHORT"

    try:
        mark = staged.get_mark_price(symbol, api, sec, recv)
    except Exception as exc:
        print(f"{grid.YELLOW}BE protect mark skip: {exc}{grid.RESET}")
        return
    if mark <= 0:
        return

    pnl = staged.profit_pct(entry, mark, side_is_long)
    state = staged.load_state(symbol.upper())
    already = bool(state.get("be_protect_armed")) or staged.find_our_algo(
        symbol, "BE", api, sec, recv,
    ) is not None

    if pnl < arm_pct and not already:
        print(
            f"{grid.DIM}BE protect wait · {side} pnl={pnl:+.3f}% "
            f"(need ≥{arm_pct:g}% → SL @ entry+{lock_pct:g}%){grid.RESET}"
        )
        return

    if pnl < arm_pct and already:
        # Keep / resync SL even if price pulled back toward entry
        print(
            f"{grid.DIM}BE protect armed · {side} pnl={pnl:+.3f}% · "
            f"holding SL @ entry+{lock_pct:g}%{grid.RESET}"
        )

    # Pure lock % from live avg entry (no fee buffer).
    be_args = argparse.Namespace(
        dry_run=bool(getattr(args, "dry_run", False)),
        be_profit_pct=lock_pct,
        recv_window=recv,
    )
    state.update({
        "phase": "be_protect",
        "symbol": symbol.upper(),
        "is_long": side_is_long,
        "entry_anchor": float(entry),
        "entry": float(entry),
        "be_protect_armed": True,
        "be_arm_pct": arm_pct,
        "be_profit_pct": lock_pct,
    })
    first_arm = not already
    state = staged._ensure_be_algo(
        symbol, side_is_long, qty, state, be_args, hedge, api, sec, filt,
        close_if_triggered=True,
    )
    state["be_protect_armed"] = True
    staged.save_state(symbol.upper(), state)

    if first_arm and state.get("algo_ids", {}).get("be"):
        print(
            f"{grid.BOLD}{grid.GREEN}✓ BE protect · {side} pnl={pnl:+.3f}% ≥ {arm_pct:g}% "
            f"→ SL @ entry+{lock_pct:g}% (no trail){grid.RESET}"
        )
        try:
            import telegram_notify as telegram

            lev = grid.get_symbol_leverage(symbol, api, sec, recv)
            _, upnl = staged.position_pnl(symbol, side_is_long, hedge, api, sec, recv)
            be_px = float(state.get("be_price") or 0)
            telegram.notify_profit_lock_sl(
                symbol.upper(), side, qty, entry, be_px,
                closed_pct=0.0,
                runner_pct=100.0,
                trigger=f"pnl≥{arm_pct:g}% → SL entry+{lock_pct:g}%",
                closed_qty=0.0,
                leverage=lev,
                pnl_usdt=upnl,
            )
        except Exception:
            pass
    elif already:
        be_px = float(state.get("be_price") or 0)
        print(
            f"{grid.DIM}BE protect sync · {side} qty={qty:g} "
            f"SL@{grid.price_fmt(be_px)} (entry+{lock_pct:g}%){grid.RESET}"
        )


def sync_flat(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    """Clear BE protect algo + state when flat."""
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    n = staged.cancel_our_algos(symbol, "BE", api, sec, recv)
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
        print(f"Cleared {n} BE protect algo(s) while flat.")
