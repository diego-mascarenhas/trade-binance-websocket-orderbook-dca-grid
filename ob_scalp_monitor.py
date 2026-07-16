#!/usr/bin/env python3
"""Poll Binance + scalp logs for live ZECUSDT (or any symbol) monitoring."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from ob_scalp_pnl import format_pnl_plain, load_pnl_stats
from ob_scalp_recovery import format_status, load_state, journal_path
from orderbook_dca_grid import (
    _resolve_hedge,
    _signed_request,
    get_position,
    load_env_file,
    load_keys,
    price_fmt,
)
from ob_signals import profit_pct


def _tail_lines(path: Path, n: int = 5) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[-n:]


def snapshot(symbol: str, api: str, sec: str, recv: int, hedge: bool) -> str:
    sym = symbol.upper()
    mark_resp = _signed_request("GET", "/fapi/v1/premiumIndex", {"symbol": sym}, api, sec, recv)
    mark = float(mark_resp.get("markPrice", 0) or 0)

    ql, el = get_position(sym, True, hedge, api, sec, recv)
    qs, es = get_position(sym, False, hedge, api, sec, recv)

    lines = [f"\n{'='*60}", f"{datetime.now().strftime('%H:%M:%S')}  {sym}  mark {price_fmt(mark)}"]

    if ql > 0:
        pnl = profit_pct(el, mark, True)
        lines.append(f"  LONG  qty={ql:g} entry={price_fmt(el)}  pnl {pnl:+.3f}%")
    if qs > 0:
        pnl = profit_pct(es, mark, False)
        lines.append(f"  SHORT qty={qs:g} entry={price_fmt(es)}  pnl {pnl:+.3f}%")
    if ql <= 0 and qs <= 0:
        lines.append("  flat")

    stats = load_pnl_stats(sym)
    lines.append(f"  {format_pnl_plain(stats)}")

    recovery = load_state(sym)
    if recovery.level > 0 or recovery.cumulative_loss_usdt > 0:
        lines.append(f"  {format_status(recovery)}")

    recent = _tail_lines(journal_path(sym), 3)
    if recent:
        lines.append("  journal:")
        for row in recent:
            lines.append(f"    {row}")

    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Live scalp monitor via Binance API")
    p.add_argument("symbol", nargs="?", default="ZECUSDT")
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--count", type=int, default=0, help="0 = forever")
    p.add_argument("--recv-window", type=int, default=15000)
    args = p.parse_args()

    load_env_file(None)
    api, sec = load_keys(None)
    if not api or not sec:
        print("No API keys", file=sys.stderr)
        sys.exit(1)

    class _A:
        position_mode = "auto"
        recv_window = args.recv_window
        env_file = None

    hedge = _resolve_hedge(_A(), api, sec)
    sym = args.symbol.upper()
    n = 0
    try:
        while True:
            print(snapshot(sym, api, sec, args.recv_window, hedge), flush=True)
            n += 1
            if args.count > 0 and n >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
