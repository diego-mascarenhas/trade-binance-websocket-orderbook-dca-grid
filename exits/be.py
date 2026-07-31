"""Exit: protect-BE, optional post-BE trail.

When unrealized profit ≥ --be-arm-pct (default 1%), place a reduce-only
STOP_MARKET on the full position at entry ± --be-profit-pct (default 0.3%).
Lock is pure profit % from entry — no fee buffer.

After BE is armed, optional ``--post-be trail`` waits for
``--post-be-arm-pct`` (default 2%) then places TRAILING_STOP_MARKET with
``--post-be-callback`` (default 0.8%), keeping the BE SL as a floor.

Used alone via `--exit be`, or as the protect layer under `--exit structure`
(TP remains EQH/EQL). DCA grid stays active; BE/trail qty are resynced if size grows.
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


def post_be_mode(args: argparse.Namespace) -> str:
    """none | trail — what to do after BE protect is armed."""
    raw = getattr(args, "post_be", None)
    if raw is None or str(raw).strip() == "":
        raw = os.getenv("POST_BE", "none")
    mode = str(raw).strip().lower()
    if mode in ("trail", "trailing", "tr"):
        return "trail"
    return "none"


def post_be_arm_pct(args: argparse.Namespace) -> float:
    v = getattr(args, "post_be_arm_pct", None)
    if v is not None:
        return float(v)
    return _env_float("POST_BE_ARM_PCT", 2.0)


def post_be_callback(args: argparse.Namespace) -> float:
    v = getattr(args, "post_be_callback", None)
    if v is not None:
        return float(v)
    return _env_float("POST_BE_CALLBACK", 0.8)


def _place_immediate_trail(
    symbol: str,
    is_long: bool,
    qty: float,
    callback: float,
    filt: dict[str, Decimal],
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> dict:
    """TRAILING_STOP_MARKET that activates immediately (no activatePrice)."""
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    step = filt["step_size"]
    qty_dp = grid._dec_places(step)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        raise ValueError("qty too small for post-BE trail")
    qty_str = f"{qty_d:.{qty_dp}f}"
    cb = max(0.1, min(10.0, float(callback)))
    close_side = "SELL" if is_long else "BUY"

    staged.cancel_our_algos(symbol, "TR", api, sec, recv)

    params: dict = {
        "algoType": "CONDITIONAL",
        "symbol": symbol.upper(),
        "side": close_side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": qty_str,
        "callbackRate": cb,
        "workingType": "CONTRACT_PRICE",
        "clientAlgoId": staged._algo_client_tag("TR", symbol),
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    else:
        params["reduceOnly"] = "true"
    return grid._signed_request("POST", "/fapi/v1/algoOrder", params, api, sec, recv)


def _maybe_post_be_trail(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    mark: float,
    pnl: float,
    state: dict,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> dict:
    """After BE is armed: optionally upgrade to trail at a higher profit."""
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    if post_be_mode(args) != "trail":
        return state

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    arm = post_be_arm_pct(args)
    cb = post_be_callback(args)
    side = "LONG" if side_is_long else "SHORT"
    existing_tr = staged.find_our_algo(symbol, "TR", api, sec, recv)
    trail_armed = bool(state.get("post_be_trail_armed")) or existing_tr is not None

    if pnl < arm and not trail_armed:
        print(
            f"{grid.DIM}Post-BE trail wait · {side} pnl={pnl:+.3f}% "
            f"(need ≥{arm:g}% → trail cb={cb:g}%){grid.RESET}"
        )
        return state

    step = filt["step_size"]
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return state

    need_replace = False
    if existing_tr is not None:
        try:
            prev_q = float(existing_tr.get("quantity") or 0)
            need_replace = abs(prev_q - float(qty_d)) >= float(step) / 2
        except (TypeError, ValueError):
            need_replace = True

    if trail_armed and not need_replace:
        print(
            f"{grid.DIM}Post-BE trail armed · {side} pnl={pnl:+.3f}% · "
            f"cb={cb:g}% (BE floor kept){grid.RESET}"
        )
        state["post_be_trail_armed"] = True
        return state

    if bool(getattr(args, "dry_run", False)):
        print(
            f"{grid.YELLOW}DRY-RUN post-BE trail · {side} "
            f"pnl≥{arm:g}% → TRAILING cb={cb:g}%{grid.RESET}"
        )
        state["post_be_trail_armed"] = True
        return state

    try:
        resp = _place_immediate_trail(
            symbol, side_is_long, float(qty_d), cb, filt, hedge, api, sec, recv,
        )
    except Exception as exc:
        print(f"{grid.RED}✗ Post-BE trail failed: {exc}{grid.RESET}")
        return state

    first = not trail_armed
    algo_ids = dict(state.get("algo_ids") or {})
    algo_ids["trail"] = resp.get("algoId")
    state["algo_ids"] = algo_ids
    state["post_be_trail_armed"] = True
    state["post_be_arm_pct"] = arm
    state["post_be_callback"] = cb
    state["phase"] = "be_protect_trail"

    if first:
        print(
            f"{grid.BOLD}{grid.GREEN}✓ Post-BE trail · {side} pnl={pnl:+.3f}% ≥ {arm:g}% "
            f"→ TRAILING cb={cb:g}% (BE floor kept) "
            f"algoId={resp.get('algoId')}{grid.RESET}"
        )
        try:
            import telegram_notify as telegram

            lev = grid.get_symbol_leverage(symbol, api, sec, recv)
            _, upnl = staged.position_pnl(symbol, side_is_long, hedge, api, sec, recv)
            telegram.notify_trail_started(
                symbol.upper(), side, float(qty_d), mark, cb,
                entry=entry, leverage=lev, pnl_usdt=upnl,
            )
        except Exception:
            pass
    else:
        print(
            f"{grid.DIM}Post-BE trail refreshed · {side} qty={float(qty_d):g} "
            f"cb={cb:g}%{grid.RESET}"
        )
    return state


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
        print(
            f"{grid.DIM}BE protect armed · {side} pnl={pnl:+.3f}% · "
            f"holding SL @ entry+{lock_pct:g}%{grid.RESET}"
        )

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

    if first_arm and state.get("algo_ids", {}).get("be"):
        print(
            f"{grid.BOLD}{grid.GREEN}✓ BE protect · {side} pnl={pnl:+.3f}% ≥ {arm_pct:g}% "
            f"→ SL @ entry+{lock_pct:g}%{grid.RESET}"
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
                hashtag="#BE",
            )
        except Exception:
            pass
    elif already:
        be_px = float(state.get("be_price") or 0)
        print(
            f"{grid.DIM}BE protect sync · {side} qty={qty:g} "
            f"SL@{grid.price_fmt(be_px)} (entry+{lock_pct:g}%){grid.RESET}"
        )

    still_long, still_qty, still_entry = grid._detect_open_side(
        symbol, hedge, api, sec, recv, prefer_is_long=side_is_long,
    )
    if still_long is None or still_qty <= 0:
        staged.save_state(symbol.upper(), state)
        return

    # Recompute pnl vs live mark/entry after possible BE close_if_triggered path
    try:
        mark = staged.get_mark_price(symbol, api, sec, recv)
    except Exception:
        pass
    pnl = staged.profit_pct(still_entry, mark, still_long)

    state = _maybe_post_be_trail(
        symbol, still_long, still_qty, still_entry, mark, pnl,
        state, args, hedge, api, sec, filt,
    )
    staged.save_state(symbol.upper(), state)


def sync_flat(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    """Clear BE protect + post-BE trail algos/state when flat."""
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    n_be = staged.cancel_our_algos(symbol, "BE", api, sec, recv)
    n_tr = staged.cancel_our_algos(symbol, "TR", api, sec, recv)
    staged.save_state(
        symbol.upper(),
        {
            "phase": staged.PHASE_IDLE,
            "symbol": symbol.upper(),
            "remain_qty": 0.0,
            "algo_ids": {},
            "be_protect_armed": False,
            "post_be_trail_armed": False,
        },
    )
    n = n_be + n_tr
    if n:
        print(f"Cleared {n} BE/trail protect algo(s) while flat.")
