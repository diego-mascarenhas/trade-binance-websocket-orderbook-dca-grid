#!/usr/bin/env python3
"""Binance USDT-M Futures scanner: top gainers, losers, and hot (volume) pairs."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

FAPI_BASE = "https://fapi.binance.com"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass(frozen=True)
class TickerRow:
    symbol: str
    last: float
    change_pct: float
    quote_volume: float
    high: float
    low: float
    trades: int


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "futures-scan/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def trading_usdt_perps(base: str) -> set[str]:
    info = _get(f"{base}/fapi/v1/exchangeInfo")
    symbols: set[str] = set()
    for s in info.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("symbol", "").endswith("USDT")
        ):
            symbols.add(s["symbol"])
    return symbols


def fetch_tickers(base: str, allowed: set[str]) -> list[TickerRow]:
    raw = _get(f"{base}/fapi/v1/ticker/24hr")
    rows: list[TickerRow] = []
    for t in raw if isinstance(raw, list) else []:
        sym = t.get("symbol", "")
        if sym not in allowed:
            continue
        try:
            rows.append(
                TickerRow(
                    symbol=sym,
                    last=float(t["lastPrice"]),
                    change_pct=float(t["priceChangePercent"]),
                    quote_volume=float(t["quoteVolume"]),
                    high=float(t["highPrice"]),
                    low=float(t["lowPrice"]),
                    trades=int(t.get("count", 0) or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def _fmt_vol_m(x: float) -> str:
    if x >= 1_000_000_000:
        return f"{x / 1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}K"
    return f"{x:.0f}"


def _fmt_pct(x: float) -> str:
    color = GREEN if x > 0 else RED if x < 0 else DIM
    return f"{color}{x:+.2f}%{RESET}"


def print_table(title: str, rows: list[TickerRow], *, show_range: bool = False) -> None:
    print(f"\n{BOLD}{CYAN}{title}{RESET}  {DIM}({len(rows)} pairs){RESET}")
    if not rows:
        print(f"  {DIM}(none — try lowering --min-volume){RESET}")
        return
    hdr = (
        f"  {'#':>3}  {'Symbol':<16} {'Last':>12} {'24h %':>10} "
        f"{'Vol 24h':>10} {'Trades':>10}"
    )
    if show_range:
        hdr += f"  {'Range %':>9}"
    print(DIM + hdr + RESET)
    for i, r in enumerate(rows, 1):
        rng = ((r.high - r.low) / r.last * 100) if r.last > 0 and show_range else 0.0
        line = (
            f"  {i:>3}  {r.symbol:<16} {_fmt_price(r.last):>12} "
            f"{_fmt_pct(r.change_pct):>19} {_fmt_vol_m(r.quote_volume):>10} {r.trades:>10,}"
        )
        if show_range:
            line += f"  {rng:>8.1f}%"
        print(line)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Show Binance USDT-M futures gainers, losers, and hot pairs (24h).",
    )
    p.add_argument("-n", "--top", type=int, default=15, help="Rows per section (default: 15)")
    p.add_argument(
        "--min-volume",
        type=float,
        default=5_000_000,
        metavar="USDT",
        help="Min 24h quote volume to include (default: 5M USDT)",
    )
    p.add_argument("--base", default=FAPI_BASE, help="Futures REST base URL")
    p.add_argument("--json", action="store_true", help="Print JSON instead of tables")
    p.add_argument(
        "--sections",
        default="gainers,losers,hots",
        help="Comma-separated: gainers,losers,hots (default: all)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    sections = {s.strip().lower() for s in args.sections.split(",") if s.strip()}
    valid = {"gainers", "losers", "hots"}
    bad = sections - valid
    if bad:
        print(f"Unknown section(s): {', '.join(sorted(bad))}", file=sys.stderr)
        sys.exit(1)
    if not sections:
        print("No sections selected.", file=sys.stderr)
        sys.exit(1)

    try:
        allowed = trading_usdt_perps(args.base.rstrip("/"))
        tickers = fetch_tickers(args.base.rstrip("/"), allowed)
    except urllib.error.URLError as exc:
        print(f"{RED}API error: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    liquid = [t for t in tickers if t.quote_volume >= args.min_volume]
    gainers = sorted(liquid, key=lambda t: t.change_pct, reverse=True)[: max(args.top, 0)]
    losers = sorted(liquid, key=lambda t: t.change_pct)[: max(args.top, 0)]
    hots = sorted(liquid, key=lambda t: t.quote_volume, reverse=True)[: max(args.top, 0)]

    if args.json:
        out = {}
        if "gainers" in sections:
            out["gainers"] = [t.__dict__ for t in gainers]
        if "losers" in sections:
            out["losers"] = [t.__dict__ for t in losers]
        if "hots" in sections:
            out["hots"] = [t.__dict__ for t in hots]
        print(json.dumps(out, indent=2))
        return

    print(f"{BOLD}Binance USDT-M Futures · 24h scan{RESET}")
    print(
        f"{DIM}Perpetuals TRADING · min vol {_fmt_vol_m(args.min_volume)} USDT · "
        f"top {args.top}{RESET}"
    )

    if "gainers" in sections:
        print_table("▲ Gainers", gainers, show_range=True)
    if "losers" in sections:
        print_table("▼ Losers", losers, show_range=True)
    if "hots" in sections:
        print_table("🔥 Hot (volume)", hots)

    print(
        f"\n{DIM}Tip: dca SYMBOL  or  python3 botctl.py start SYMBOL{RESET}\n"
    )


if __name__ == "__main__":
    main()
