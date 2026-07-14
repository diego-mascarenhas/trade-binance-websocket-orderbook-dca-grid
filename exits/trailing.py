"""Default exit: single trailing TP on the opposite order-book wall."""

from __future__ import annotations

import argparse
from decimal import Decimal


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

    grid._manage_tp_once(symbol, side_is_long, qty, entry, args, hedge, api, sec, filt)
