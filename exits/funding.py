"""Funding fee guard: block new opens / flatten before expensive funding.

Binance USD-M funding every 8h (nextFundingTime on premiumIndex).

Who pays:
  · rate > 0 → LONGs pay SHORTs
  · rate < 0 → SHORTs pay LONGs

When we would *pay* and |rate| ≥ FUNDING_PAY_MAX_PCT (default 0.3%):
  · skip new grid arms / pumpstall ★ launches
  · with an open position, market-close FUNDING_CLOSE_LEAD_MIN minutes
    before settlement (default 10)

Env / CLI:
  FUNDING_GUARD=1
  FUNDING_PAY_MAX_PCT=0.3
  FUNDING_CLOSE_LEAD_MIN=10
"""

from __future__ import annotations

import argparse
import os
import time
import urllib.parse
from decimal import Decimal
from typing import Any

_CLOSE_REASONS: dict[str, str] = {}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None and getattr(args, "funding_guard", None) is not None:
        return bool(args.funding_guard)
    return _env_bool("FUNDING_GUARD", True)


def pay_max_pct(args: argparse.Namespace | None = None) -> float:
    """Min |funding %%| we would pay to trigger block/close (default 0.3)."""
    if args is not None:
        v = getattr(args, "funding_pay_max_pct", None)
        if v is not None:
            return max(0.0, float(v))
    return max(0.0, _env_float("FUNDING_PAY_MAX_PCT", 0.3))


def close_lead_min(args: argparse.Namespace | None = None) -> float:
    """Minutes before nextFundingTime to flatten (default 10). 0 = close ASAP when pay≥max."""
    if args is not None:
        v = getattr(args, "funding_close_lead_min", None)
        if v is not None:
            return max(0.0, float(v))
    return max(0.0, _env_float("FUNDING_CLOSE_LEAD_MIN", 10.0))


def pop_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.pop(symbol.upper(), None)


def peek_close_reason(symbol: str) -> str | None:
    return _CLOSE_REASONS.get(symbol.upper())


def fetch_premium(symbol: str) -> dict[str, Any] | None:
    """Return {rate_pct, next_funding_ms, mark} from /fapi/v1/premiumIndex."""
    try:
        from futures_scan import FAPI_BASE, _get

        prem = _get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex?"
            f"{urllib.parse.urlencode({'symbol': symbol.upper()})}"
        )
    except Exception:
        return None
    if not isinstance(prem, dict):
        return None
    try:
        rate = float(prem.get("lastFundingRate", 0) or 0) * 100.0  # → %%
        nxt = int(float(prem.get("nextFundingTime", 0) or 0))
        mark = float(prem.get("markPrice", 0) or 0)
    except (TypeError, ValueError):
        return None
    return {"rate_pct": rate, "next_funding_ms": nxt, "mark": mark}


def pay_rate_pct(is_long: bool, rate_pct: float) -> float:
    """Funding %% we would *pay* this window (0 if we receive)."""
    if rate_pct > 0 and is_long:
        return float(rate_pct)
    if rate_pct < 0 and not is_long:
        return float(abs(rate_pct))
    return 0.0


def seconds_to_funding(next_funding_ms: int, *, now: float | None = None) -> float | None:
    if not next_funding_ms or next_funding_ms <= 0:
        return None
    t = time.time() if now is None else float(now)
    return max(0.0, next_funding_ms / 1000.0 - t)


def entry_blocked_by_funding(
    symbol: str,
    is_long: bool,
    args: argparse.Namespace | None = None,
) -> tuple[bool, str]:
    """True when a new open would pay funding ≥ threshold this window."""
    if not enabled(args):
        return False, ""
    max_pct = pay_max_pct(args)
    if max_pct <= 0:
        return False, ""
    prem = fetch_premium(symbol)
    if not prem:
        return False, ""
    pay = pay_rate_pct(is_long, float(prem["rate_pct"]))
    if pay + 1e-12 < max_pct:
        return False, ""
    side = "LONG" if is_long else "SHORT"
    secs = seconds_to_funding(int(prem["next_funding_ms"]))
    eta = f" · next in {secs / 60.0:.0f}m" if secs is not None else ""
    return True, (
        f"{side} would pay funding {pay:.3f}% ≥ {max_pct:g}% "
        f"(rate {float(prem['rate_pct']):+.4f} %{eta})"
    )


def should_close_for_funding(
    symbol: str,
    is_long: bool,
    args: argparse.Namespace | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """True when open position should flatten to avoid paying funding."""
    if not enabled(args):
        return False, "", None
    max_pct = pay_max_pct(args)
    if max_pct <= 0:
        return False, "", None
    prem = fetch_premium(symbol)
    if not prem:
        return False, "", None
    pay = pay_rate_pct(is_long, float(prem["rate_pct"]))
    if pay + 1e-12 < max_pct:
        return False, "", None
    secs = seconds_to_funding(int(prem["next_funding_ms"]))
    if secs is None:
        return False, "", None
    lead_s = close_lead_min(args) * 60.0
    # lead=0 → close as soon as pay≥max (still before next funding)
    if secs > lead_s and lead_s > 0:
        return False, "", prem
    if secs <= 0:
        # Already past / at settlement — too late for this window
        return False, "", prem
    side = "LONG" if is_long else "SHORT"
    why = (
        f"funding pay {pay:.3f}% in {secs / 60.0:.1f}m "
        f"(rate {float(prem['rate_pct']):+.4f}% · {side})"
    )
    return True, why, prem


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
) -> bool:
    """If funding close fires, flatten and return True (caller should skip exit)."""
    if qty <= 0:
        return False
    fire, why, prem = should_close_for_funding(symbol, side_is_long, args)
    if not fire:
        # Quiet status when close is approaching but not yet in lead window
        if prem and enabled(args):
            pay = pay_rate_pct(side_is_long, float(prem["rate_pct"]))
            max_pct = pay_max_pct(args)
            secs = seconds_to_funding(int(prem["next_funding_ms"]))
            if pay >= max_pct and secs is not None:
                lead_s = close_lead_min(args) * 60.0
                if lead_s > 0 and secs <= lead_s * 3:  # warn within 3× lead
                    import orderbook_dca_grid as grid

                    print(
                        f"{grid.DIM}Funding watch · pay {pay:.3f}% in "
                        f"{secs / 60.0:.0f}m (close ≤{close_lead_min(args):g}m){grid.RESET}"
                    )
        return False

    import orderbook_dca_grid as grid
    from exits.structure import cancel_close_algos

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    side = "LONG" if side_is_long else "SHORT"
    print(f"{grid.BOLD}{grid.YELLOW}✓ Funding guard · {side} close — {why}{grid.RESET}")
    try:
        try:
            grid.cancel_all_symbol_orders(symbol, api, sec, recv)
        except Exception as exc:
            print(f"{grid.YELLOW}Cancel open orders: {exc}{grid.RESET}")
        try:
            import orderbook_staged_exit as staged

            staged.cancel_all_staged_algos(symbol, api, sec, recv)
        except Exception:
            pass
        cancel_close_algos(symbol, side_is_long, api, sec, recv)
        closed = grid.market_close_position(
            symbol, side_is_long, qty, hedge, filt, api, sec, recv,
        )
        print(f"{grid.GREEN}✓ Market-closed {closed:g} (funding){grid.RESET}")
        _CLOSE_REASONS[symbol.upper()] = f"funding · {why}"
        time.sleep(0.35)
        return True
    except Exception as exc:
        print(f"{grid.RED}✗ Funding close failed: {exc}{grid.RESET}")
        return False
