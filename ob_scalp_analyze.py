#!/usr/bin/env python3
"""Analyze ZEC (or any) scalp journal + live bars to suggest parameters."""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass

from ob_bars import BarBuilder, depth_to_levels
from ob_signals import SignalConfig, entry_signal
from orderbook_dca_grid import fetch_depth, load_env_file


@dataclass
class TradeRow:
    ts: str
    kind: str
    direction: str
    gross: float | None
    pnl_usdt: float | None
    imb: float | None = None


def parse_journal(path: str) -> list[TradeRow]:
    rows: list[TradeRow] = []
    if not path:
        return rows
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return rows
    for line in text.splitlines():
        m = re.search(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (OPEN|SL|TP|FLIP|MAXBARS) (LONG|SHORT)"
            r"(?:.*gross=([+-]?\d+\.?\d*)%)?(?:.*pnl=([+-]?\d+\.?\d*) USDT)?",
            line,
        )
        if not m:
            continue
        rows.append(
            TradeRow(
                ts=m.group(1),
                kind=m.group(2),
                direction=m.group(3),
                gross=float(m.group(4)) if m.group(4) else None,
                pnl_usdt=float(m.group(5)) if m.group(5) else None,
            )
        )
    return rows


def sample_bars(symbol: str, *, bars: int, bar_sec: float, band_pct: float) -> list[dict]:
    out: list[dict] = []
    builder = BarBuilder(bar_sec=bar_sec, band_pct=band_pct)
    builder.start_bar(time.time())
    deadline = time.time() + bar_sec * bars + 5
    while len(out) < bars and time.time() < deadline:
        depth = fetch_depth(symbol, 100)
        bids, asks = depth_to_levels(depth)
        bar = builder.add_sample(bids, asks, time.time())
        if bar is None:
            time.sleep(2)
            continue
        sig = entry_signal(bar, SignalConfig())
        out.append({
            "imb": bar.imbalance * 100,
            "mid_chg": bar.mid_change_pct(),
            "signal": sig,
            "mid": bar.mid_c,
        })
        builder.reset_after_bar(time.time())
        time.sleep(0.5)
    return out


def recommend(trades: list[TradeRow], bars: list[dict]) -> str:
    lines = ["## Patrones detectados", ""]

    if trades:
        wins = [t for t in trades if t.pnl_usdt is not None and t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt is not None and t.pnl_usdt <= 0]
        lines.append(f"- Trades journal: {len(trades)} eventos · {len(wins)} wins · {len(losses)} losses")
        if losses:
            dirs = {t.direction for t in losses}
            lines.append(f"- Pérdidas en: {', '.join(sorted(dirs))}")
            avg_sl = sum(t.gross or 0 for t in losses if t.gross) / max(1, len([t for t in losses if t.gross]))
            lines.append(f"- SL medio: {avg_sl:+.3f}%")
        if wins:
            avg_tp = sum(t.gross or 0 for t in wins if t.gross) / max(1, len([t for t in wins if t.gross]))
            lines.append(f"- TP medio: {avg_tp:+.3f}%")

        # Direction flip losses (old bug)
        flips = 0
        last_dir = ""
        for t in trades:
            if t.kind == "OPEN":
                if last_dir and t.direction != last_dir:
                    flips += 1
                last_dir = t.direction
        if flips:
            lines.append(f"- ⚠ {flips} flip(s) de dirección entre trades (martingale sin lock pierde)")

    if bars:
        sig_bars = [b for b in bars if b["signal"]]
        lines.append(f"\n## Muestreo live ({len(bars)} barras)")
        lines.append(f"- Señales: {len(sig_bars)}/{len(bars)}")
        if sig_bars:
            for b in sig_bars[-5:]:
                lines.append(
                    f"  · {b['signal'].upper()} imb={b['imb']:.1f}% mid_chg={b['mid_chg']:+.3f}%"
                )
        neutral = sum(1 for b in bars if 45 <= b["imb"] <= 55)
        lines.append(f"- Barras neutrales (45–55% imb): {neutral}/{len(bars)}")

    lines.extend([
        "",
        "## Config recomendada (ZEC + martingale con lock)",
        "",
        "```bash",
        "obscalp ZECUSDT --execute --recover --recover-max-level 3 \\",
        "  --imb-long 0.58 --imb-short 0.42 \\",
        "  --momentum-min-pct 0.02 \\",
        "  --tp-pct 0.30 --sl-pct 0.12 \\",
        "  --entry-cooldown-sec 60 --bar-sec 60",
        "```",
        "",
        "`.env` sugerido:",
        "```",
        "OB_RECOVER=1",
        "OB_RECOVER_MAX_LEVEL=3",
        "OB_IMB_LONG=0.58",
        "OB_IMB_SHORT=0.42",
        "OB_MOMENTUM_MIN_PCT=0.02",
        "OB_TP_PCT=0.30",
        "OB_SL_PCT=0.12",
        "OB_ENTRY_COOLDOWN_SEC=60",
        "```",
        "",
        "**Reglas:**",
        "- NO usar `--no-momentum` en ZEC",
        "- Tras SL, martingale solo **misma dirección** (locked_side)",
        "- Max level 3 = 8x base (~40 USDT en min size)",
        "- TP/SL ratio ~2.5:1 para que 1 TP cubra 2 SL al mismo size",
    ])
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Scalp pattern analysis")
    p.add_argument("symbol", nargs="?", default="ZECUSDT")
    p.add_argument("--journal", default="")
    p.add_argument("--sample-bars", type=int, default=8)
    p.add_argument("--bar-sec", type=float, default=60.0)
    args = p.parse_args()
    load_env_file(None)

    sym = args.symbol.upper()
    journal = args.journal or f".run/logs/{sym}/scalp_trades.log"
    trades = parse_journal(journal)
    print(f"Sampling {args.sample_bars} bars on {sym}…", file=sys.stderr)
    bars = sample_bars(sym, bars=args.sample_bars, bar_sec=args.bar_sec, band_pct=1.0)
    print(recommend(trades, bars))


if __name__ == "__main__":
    main()
