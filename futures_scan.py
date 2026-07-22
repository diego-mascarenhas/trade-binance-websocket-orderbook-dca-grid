#!/usr/bin/env python3
"""Binance USDT-M Futures scanner: gainers, losers, hots, volatility & trend."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

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

    @property
    def range_pct(self) -> float:
        return ((self.high - self.low) / self.last * 100) if self.last > 0 else 0.0


@dataclass
class SymbolInsight:
    symbol: str
    last: float
    change_24h: float
    change_4h: float
    change_1h: float
    range_24h: float
    range_1h: float
    vol_avg_1h: float
    quote_volume: float
    book_direction: str
    book_bid_share: float
    funding_pct: float
    trend: str
    trend_note: str


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


def fetch_klines(base: str, symbol: str, interval: str, limit: int) -> list[list]:
    q = urllib.parse.urlencode({"symbol": symbol.upper(), "interval": interval, "limit": limit})
    data = _get(f"{base}/fapi/v1/klines?{q}")
    return data if isinstance(data, list) else []


def pct_change(from_px: float, to_px: float) -> float:
    return ((to_px - from_px) / from_px * 100) if from_px else 0.0


def book_imbalance(bids: list[list[float]], asks: list[list[float]], mid: float, band_pct: float = 1.0) -> dict:
    lo = mid * (1 - band_pct / 100)
    hi = mid * (1 + band_pct / 100)
    bid_vol = sum(q for p, q in bids if p >= lo)
    ask_vol = sum(q for p, q in asks if p <= hi)
    total = bid_vol + ask_vol
    imb = (bid_vol / total) if total else 0.5
    direction = "LONG" if bid_vol >= ask_vol else "SHORT"
    return {"direction": direction, "bid_share": imb * 100, "bid_vol": bid_vol, "ask_vol": ask_vol}


def classify_trend(change_1h: float, change_4h: float, change_24h: float, book_dir: str) -> tuple[str, str]:
    scores = [change_1h, change_4h, change_24h]
    bulls = sum(1 for x in scores if x > 0.15)
    bears = sum(1 for x in scores if x < -0.15)
    book_bull = book_dir == "LONG"

    if bulls >= 2 and book_bull:
        return "BULLISH", "1h/4h/24h mostly up · book bids heavy"
    if bears >= 2 and not book_bull:
        return "BEARISH", "1h/4h/24h mostly down · book asks heavy"
    if bulls >= 2:
        return "BULLISH", "momentum up · book mixed"
    if bears >= 2:
        return "BEARISH", "momentum down · book mixed"
    if book_bull and change_1h > 0:
        return "LEAN LONG", "short-term bid support"
    if not book_bull and change_1h < 0:
        return "LEAN SHORT", "short-term ask pressure"
    return "NEUTRAL", "mixed timeframes / flat"


def build_insight(base: str, symbol: str, ticker: TickerRow | None = None) -> SymbolInsight:
    sym = symbol.upper()
    if ticker is None:
        tickers = fetch_tickers(base, {sym})
        if not tickers:
            raise ValueError(f"No 24h ticker for {sym}")
        ticker = tickers[0]

    k1 = fetch_klines(base, sym, "1h", 24)
    k4 = fetch_klines(base, sym, "4h", 2)

    change_1h = 0.0
    range_1h = 0.0
    vol_avg_1h = 0.0
    if len(k1) >= 2:
        change_1h = pct_change(float(k1[-2][4]), float(k1[-1][4]))
        hi, lo, last_close = float(k1[-1][2]), float(k1[-1][3]), float(k1[-1][4])
        range_1h = ((hi - lo) / last_close * 100) if last_close else 0.0
    if k1:
        vol_avg_1h = sum((float(k[2]) - float(k[3])) / float(k[4]) * 100 for k in k1 if float(k[4])) / len(k1)

    change_4h = pct_change(float(k4[-2][4]), float(k4[-1][4])) if len(k4) >= 2 else 0.0

    depth = _get(f"{base}/fapi/v1/depth?{urllib.parse.urlencode({'symbol': sym, 'limit': 50})}")
    bids = [[float(p), float(q)] for p, q in depth.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in depth.get("asks", [])]
    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else ticker.last
    book = book_imbalance(bids, asks, mid)

    funding_pct = 0.0
    try:
        prem = _get(f"{base}/fapi/v1/premiumIndex?{urllib.parse.urlencode({'symbol': sym})}")
        funding_pct = float(prem.get("lastFundingRate", 0) or 0) * 100
    except (TypeError, ValueError, urllib.error.URLError):
        pass

    trend, note = classify_trend(change_1h, change_4h, ticker.change_pct, book["direction"])

    return SymbolInsight(
        symbol=sym,
        last=ticker.last,
        change_24h=ticker.change_pct,
        change_4h=change_4h,
        change_1h=change_1h,
        range_24h=ticker.range_pct,
        range_1h=range_1h,
        vol_avg_1h=vol_avg_1h,
        quote_volume=ticker.quote_volume,
        book_direction=book["direction"],
        book_bid_share=book["bid_share"],
        funding_pct=funding_pct,
        trend=trend,
        trend_note=note,
    )


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


def _trend_badge(trend: str) -> str:
    t = trend.upper()
    if "BULL" in t or t == "LONG":
        return f"{GREEN}{t}{RESET}"
    if "BEAR" in t or "SHORT" in t:
        return f"{RED}{t}{RESET}"
    return f"{YELLOW}{t}{RESET}"


def print_symbol(ins: SymbolInsight) -> None:
    print(f"\n{BOLD}{CYAN}{ins.symbol}{RESET}  @ {_fmt_price(ins.last)}")
    print(f"  {BOLD}Trend{RESET}     {_trend_badge(ins.trend)}  {DIM}{ins.trend_note}{RESET}")
    print(f"  {BOLD}Momentum{RESET}  1h {_fmt_pct(ins.change_1h)}  ·  4h {_fmt_pct(ins.change_4h)}  ·  24h {_fmt_pct(ins.change_24h)}")
    print(
        f"  {BOLD}Volatility{RESET}  24h range {ins.range_24h:.1f}%  ·  "
        f"1h range {ins.range_1h:.1f}%  ·  avg 1h range {ins.vol_avg_1h:.1f}%"
    )
    print(
        f"  {BOLD}Book (now){RESET}  AUTO → {ins.book_direction}  "
        f"({ins.book_bid_share:.0f}% bid share in ±1%)"
    )
    print(
        f"  {BOLD}Activity{RESET}  vol 24h {_fmt_vol_m(ins.quote_volume)} USDT  ·  "
        f"funding {ins.funding_pct:+.4f}% / 8h"
    )
    print(f"\n  {DIM}Bot: dca {ins.symbol}  or  dca {ins.symbol} {ins.book_direction.lower()}{RESET}\n")


def print_table(title: str, rows: list[TickerRow], insights: dict[str, SymbolInsight] | None = None) -> None:
    print(f"\n{BOLD}{CYAN}{title}{RESET}  {DIM}({len(rows)} pairs){RESET}")
    if not rows:
        print(f"  {DIM}(none — try lowering --min-volume){RESET}")
        return
    hdr = (
        f"  {'#':>3}  {'Symbol':<16} {'Last':>12} {'24h %':>10} "
        f"{'Range%':>7} {'Trend':>12} {'Vol 24h':>10}"
    )
    print(DIM + hdr + RESET)
    for i, r in enumerate(rows, 1):
        ins = insights.get(r.symbol) if insights else None
        if ins:
            trend_s = _trend_badge(ins.trend)
        else:
            trend_s = f"{GREEN}↑{RESET}" if r.change_pct > 1 else f"{RED}↓{RESET}" if r.change_pct < -1 else f"{DIM}→{RESET}"
        print(
            f"  {i:>3}  {r.symbol:<16} {_fmt_price(r.last):>12} "
            f"{_fmt_pct(r.change_pct):>19} {r.range_pct:>6.1f}% {trend_s:>21} "
            f"{_fmt_vol_m(r.quote_volume):>10}"
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Binance USDT-M futures: gainers, losers, hots, volatility & trend.",
    )
    p.add_argument("symbol", nargs="?", help="Single symbol detail, e.g. 1000PEPEUSDT")
    p.add_argument("-n", "--top", type=int, default=15, help="Rows per section (default: 15)")
    p.add_argument("--min-volume", type=float, default=5_000_000, metavar="USDT",
                   help="Min 24h quote volume (default: 5M USDT)")
    p.add_argument("--base", default=FAPI_BASE, help="Futures REST base URL")
    p.add_argument("--json", action="store_true", help="Print JSON")
    p.add_argument("--sections", default="gainers,losers,hots",
                   help="gainers,losers,hots,volatile (default: gainers,losers,hots)")
    p.add_argument("--with-trend", action="store_true",
                   help="Fetch 1h/book trend per row in lists (slower)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base = args.base.rstrip("/")

    if args.symbol:
        sym = args.symbol.upper()
        try:
            allowed = trading_usdt_perps(base)
            if sym not in allowed:
                print(f"{YELLOW}Warning: {sym} not in TRADING perpetuals.{RESET}", file=sys.stderr)
            ins = build_insight(base, sym)
        except (urllib.error.URLError, ValueError) as exc:
            print(f"{RED}Error: {exc}{RESET}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(asdict(ins), indent=2))
        else:
            print_symbol(ins)
        return

    sections = {s.strip().lower() for s in args.sections.split(",") if s.strip()}
    valid = {"gainers", "losers", "hots", "volatile"}
    bad = sections - valid
    if bad:
        print(f"Unknown section(s): {', '.join(sorted(bad))}", file=sys.stderr)
        sys.exit(1)

    try:
        allowed = trading_usdt_perps(base)
        tickers = fetch_tickers(base, allowed)
    except urllib.error.URLError as exc:
        print(f"{RED}API error: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    liquid = [t for t in tickers if t.quote_volume >= args.min_volume]
    gainers = sorted(liquid, key=lambda t: t.change_pct, reverse=True)[: max(args.top, 0)]
    losers = sorted(liquid, key=lambda t: t.change_pct)[: max(args.top, 0)]
    hots = sorted(liquid, key=lambda t: t.quote_volume, reverse=True)[: max(args.top, 0)]
    volatile = sorted(liquid, key=lambda t: t.range_pct, reverse=True)[: max(args.top, 0)]

    insights: dict[str, SymbolInsight] = {}
    if args.with_trend:
        seen: set[str] = set()
        for bucket in (gainers, losers, hots, volatile):
            for r in bucket:
                if r.symbol in seen:
                    continue
                seen.add(r.symbol)
                try:
                    insights[r.symbol] = build_insight(base, r.symbol, r)
                except urllib.error.URLError:
                    pass

    if args.json:
        def row_json(t: TickerRow) -> dict:
            return asdict(insights[t.symbol]) if t.symbol in insights else t.__dict__
        out: dict = {}
        if "gainers" in sections:
            out["gainers"] = [row_json(t) for t in gainers]
        if "losers" in sections:
            out["losers"] = [row_json(t) for t in losers]
        if "hots" in sections:
            out["hots"] = [row_json(t) for t in hots]
        if "volatile" in sections:
            out["volatile"] = [row_json(t) for t in volatile]
        print(json.dumps(out, indent=2))
        return

    print(f"{BOLD}Binance USDT-M Futures · 24h scan{RESET}")
    print(f"{DIM}Range% = volatility · fscan SYMBOL = live trend + book{RESET}")

    if "gainers" in sections:
        print_table("▲ Gainers", gainers, insights if args.with_trend else None)
    if "losers" in sections:
        print_table("▼ Losers", losers, insights if args.with_trend else None)
    if "volatile" in sections:
        print_table("⚡ Volatile (24h range)", volatile, insights if args.with_trend else None)
    if "hots" in sections:
        print_table("🔥 Hot (volume)", hots, insights if args.with_trend else None)

    print(f"\n{DIM}Detail: fscan 1000PEPEUSDT  ·  fscan --sections volatile -n 10{RESET}\n")


if __name__ == "__main__":
    main()
