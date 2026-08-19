"""Realized-loss recovery floor for the open runner.

A reduce that books a loss (manual cut, SL slice, etc.) is stored as debt.
TP / BE / ratchet then require enough %% on the *remaining* notional to
cover that hole plus their normal edge — so a “green” close of the rest
cannot leave the whole trade red.
"""

from __future__ import annotations

KEY = "recovery_realized_usdt"


def realized_usdt(symbol: str) -> float:
    try:
        import orderbook_staged_exit as staged

        return float((staged.load_state(symbol.upper()) or {}).get(KEY) or 0)
    except Exception:
        return 0.0


def debt_usdt(symbol: str) -> float:
    return max(0.0, -realized_usdt(symbol))


def recovery_pct(
    symbol: str,
    qty: float | None = None,
    entry: float | None = None,
) -> float:
    """%% the remaining position must make to cover realized debt."""
    debt = debt_usdt(symbol)
    if debt <= 0:
        return 0.0
    try:
        q = float(qty or 0)
        e = float(entry or 0)
    except (TypeError, ValueError):
        q, e = 0.0, 0.0
    if q <= 0 or e <= 0:
        try:
            import orderbook_staged_exit as staged

            st = staged.load_state(symbol.upper()) or {}
            q = float(st.get("recovery_qty") or st.get("remain_qty") or 0)
            e = float(st.get("entry") or st.get("entry_anchor") or 0)
        except Exception:
            return 0.0
    notional = abs(q * e)
    if notional <= 0:
        return 0.0
    return 100.0 * debt / notional


def note_slice(
    symbol: str,
    *,
    closed_qty: float,
    entry: float,
    fill_price: float,
    is_long: bool,
    remain_qty: float | None = None,
) -> float:
    """Add realized PnL of a reduce. Returns new cumulative realized USDT."""
    try:
        cq = float(closed_qty)
        ent = float(entry)
        px = float(fill_price)
    except (TypeError, ValueError):
        return realized_usdt(symbol)
    if cq <= 0 or ent <= 0 or px <= 0:
        return realized_usdt(symbol)
    pnl = (px - ent) * cq if is_long else (ent - px) * cq
    try:
        import orderbook_staged_exit as staged

        sym = symbol.upper()
        st = staged.load_state(sym) or {}
        total = float(st.get(KEY) or 0) + pnl
        st[KEY] = total
        if remain_qty is not None:
            st["recovery_qty"] = float(remain_qty)
        st["entry"] = ent
        staged.save_state(sym, st)
        return total
    except Exception:
        return realized_usdt(symbol)


def clear(symbol: str) -> None:
    try:
        import orderbook_staged_exit as staged

        st = staged.load_state(symbol.upper()) or {}
        st.pop(KEY, None)
        st.pop("recovery_qty", None)
        staged.save_state(symbol.upper(), st)
    except Exception:
        pass
