#!/usr/bin/env python3
"""Show OB scalp trade history from scalp_trades.log.

Usage:
    ./obscalp-trades              # active symbol — all closes + total PnL
    ./obscalp-trades PYTHUSDT
    ./obscalp-trades -f           # live refresh (tail-style)
    ./obscalp-trades -f -n 15     # live, last 15 closes
    ./obscalp-trades --list
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ob_scalp_recovery import format_status, journal_path, load_state
from ob_scalp_stack import active_symbol, running_symbols

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"
CLEAR = "\033[H\033[2J"

_OPEN_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"OPEN (LONG|SHORT) qty=([0-9.]+) notional=([0-9.]+) level=(\d+) \((\d+)x\)"
    r"(?: trigger=(\S+))?"
)
_CLOSE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(TP|SL|TRAIL|FLIP|MAXBARS) (LONG|SHORT) qty=([0-9.]+) "
    r"entry=([0-9.eE+-]+) exit=([0-9.eE+-]+) "
    r"gross=([+-]?[0-9.]+)% pnl=([+-]?[0-9.]+) USDT "
    r"level=(\d+) streak=(\d+) cumulative=([+-]?[0-9.]+)"
    r"(?: trigger=(\S+))?"
    r"(?: → (.+))?$"
)


@dataclass
class TradeRow:
    ts: str
    kind: str  # OPEN | close reason
    side: str
    qty: float
    notional: float | None
    entry: float | None
    exit: float | None
    gross_pct: float | None
    pnl: float | None
    level: int
    mult: str
    outcome: str
    trigger: str = ""


def parse_journal(symbol: str) -> list[TradeRow]:
    path = journal_path(symbol)
    if not path.exists():
        return []
    rows: list[TradeRow] = []
    last_open_trigger = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _OPEN_RE.match(line)
        if m:
            last_open_trigger = (m.group(7) or "").strip()
            rows.append(
                TradeRow(
                    ts=m.group(1),
                    kind="OPEN",
                    side=m.group(2),
                    qty=float(m.group(3)),
                    notional=float(m.group(4)),
                    entry=None,
                    exit=None,
                    gross_pct=None,
                    pnl=None,
                    level=int(m.group(5)),
                    mult=f"{m.group(6)}x",
                    outcome="",
                    trigger=last_open_trigger,
                )
            )
            continue
        m = _CLOSE_RE.match(line)
        if m:
            outcome = (m.group(13) or "").strip()
            trig = (m.group(12) or "").strip() or last_open_trigger
            rows.append(
                TradeRow(
                    ts=m.group(1),
                    kind=m.group(2),
                    side=m.group(3),
                    qty=float(m.group(4)),
                    notional=None,
                    entry=float(m.group(5)),
                    exit=float(m.group(6)),
                    gross_pct=float(m.group(7)),
                    pnl=float(m.group(8)),
                    level=int(m.group(9)),
                    mult="",
                    outcome=outcome,
                    trigger=trig,
                )
            )
            last_open_trigger = ""
    return rows


def _pnl_color(pnl: float | None) -> str:
    if pnl is None:
        return DIM
    if pnl > 0:
        return GREEN
    if pnl < 0:
        return RED
    return DIM


def _reason_color(kind: str) -> str:
    if kind in ("TP", "TRAIL"):
        return GREEN
    if kind == "SL":
        return RED
    if kind in ("FLIP", "MAXBARS"):
        return YELLOW
    return CYAN


def _format_report(
    symbol: str,
    *,
    limit: int | None,
    show_opens: bool,
    live: bool = False,
    show_symbol_col: bool = False,
) -> str:
    sym = symbol.upper()
    all_rows = parse_journal(sym)
    closes = [r for r in all_rows if r.kind != "OPEN"]
    rows = all_rows if show_opens else closes
    if limit is not None and limit > 0:
        rows = rows[-limit:]

    lines: list[str] = []
    if not closes and not rows:
        lines.append(f"{DIM}No trades in journal for {sym}{RESET}")
        lines.append(f"  {DIM}{journal_path(sym)}{RESET}")
        if live:
            lines.append(f"\n{DIM}live · waiting for trades · Ctrl+C to stop{RESET}")
        return "\n".join(lines) + "\n"

    total_pnl = sum(r.pnl or 0.0 for r in closes)
    wins = sum(1 for r in closes if (r.pnl or 0) > 0)
    losses = sum(1 for r in closes if (r.pnl or 0) <= 0)
    t_color = GREEN if total_pnl > 0 else RED if total_pnl < 0 else DIM
    now = time.strftime("%H:%M:%S")

    head = f"{BOLD}{CYAN}{sym}{RESET}  {DIM}{len(closes)} closed trades{RESET}"
    if live:
        head += f"  {DIM}live {now}{RESET}"
        if show_symbol_col:
            head += f"  {DIM}(following active){RESET}"
    lines.append(head)
    lines.append(
        f"{BOLD}Total PnL  {t_color}{total_pnl:+.4f} USDT{RESET}  "
        f"{DIM}({wins}W/{losses}L){RESET}"
    )
    lines.append("")

    if show_symbol_col:
        hdr = (
            f"{'When':<19} {'Symbol':<10} {'Evt':<7} {'Side':<5} {'Vol USDT':>9} "
            f"{'Gross':>8} {'PnL USDT':>10}  {'Trigger':<28} Note"
        )
        sep = "-" * 108
    else:
        hdr = (
            f"{'When':<19} {'Evt':<7} {'Side':<5} {'Vol USDT':>9} "
            f"{'Gross':>8} {'PnL USDT':>10}  {'Trigger':<28} Note"
        )
        sep = "-" * 96
    lines.append(f"{DIM}{hdr}{RESET}")
    lines.append(f"{DIM}{sep}{RESET}")

    for r in rows:
        vol = r.notional
        if vol is None and r.entry is not None and r.qty > 0:
            vol = r.entry * r.qty
        vol_s = f"{vol:.2f}" if vol is not None else ""
        trig = (r.trigger or "—")[:28]
        sym_cell = f"{BOLD}{sym:<10}{RESET} " if show_symbol_col else ""
        if r.kind == "OPEN":
            note = f"qty {r.qty:g} · level {r.level} ({r.mult})"
            lines.append(
                f"{r.ts} {sym_cell}{_reason_color('OPEN')}{'OPEN':<7}{RESET} {r.side:<5} "
                f"{vol_s:>9} {'':>8} {'':>10}  {CYAN}{trig:<28}{RESET} {DIM}{note}{RESET}"
            )
            continue
        pnl_s = f"{r.pnl:+.4f}" if r.pnl is not None else ""
        gross_s = f"{r.gross_pct:+.3f}%" if r.gross_pct is not None else ""
        note = r.outcome or f"level {r.level}"
        lines.append(
            f"{r.ts} {sym_cell}{_reason_color(r.kind)}{r.kind:<7}{RESET} {r.side:<5} "
            f"{vol_s:>9} {gross_s:>8} {_pnl_color(r.pnl)}{pnl_s:>10}{RESET}  "
            f"{CYAN}{trig:<28}{RESET} {DIM}{note}{RESET}"
        )

    lines.append("")
    lines.append(
        f"{BOLD}Total PnL  {t_color}{total_pnl:+.4f} USDT{RESET}  "
        f"{DIM}{wins}W/{losses}L · {len(closes)} trades{RESET}"
    )

    # Per-trigger breakdown (attribute full tag; also expand components)
    by_tag: dict[str, list[float]] = defaultdict(list)
    by_part: dict[str, list[float]] = defaultdict(list)
    for r in closes:
        tag = r.trigger or "unknown"
        by_tag[tag].append(r.pnl or 0.0)
        for part in tag.split("+"):
            part = part.strip() or "unknown"
            by_part[part].append(r.pnl or 0.0)

    if any(t != "unknown" for t in by_tag):
        lines.append(f"\n{BOLD}By trigger tag{RESET}")
        for tag, pnls in sorted(by_tag.items(), key=lambda x: sum(x[1]), reverse=True):
            s = sum(pnls)
            w = sum(1 for p in pnls if p > 0)
            l = len(pnls) - w
            c = GREEN if s > 0 else RED if s < 0 else DIM
            lines.append(f"  {tag:<28} {c}{s:+.4f}{RESET}  {DIM}{w}W/{l}L · {len(pnls)}{RESET}")
        lines.append(f"\n{BOLD}By trigger component{RESET}  {DIM}(credit each part of a combo){RESET}")
        for part, pnls in sorted(by_part.items(), key=lambda x: sum(x[1]), reverse=True):
            s = sum(pnls)
            w = sum(1 for p in pnls if p > 0)
            l = len(pnls) - w
            c = GREEN if s > 0 else RED if s < 0 else DIM
            lines.append(f"  {part:<28} {c}{s:+.4f}{RESET}  {DIM}{w}W/{l}L · {len(pnls)}{RESET}")

    recovery = load_state(sym)
    if recovery.level > 0 or recovery.cumulative_loss_usdt > 0:
        prefix = f"{sym}  " if show_symbol_col else ""
        lines.append(f"{DIM}{prefix}{format_status(recovery)}{RESET}")
        if recovery.base_notional_usdt > 0:
            nxt = recovery.base_notional_usdt * (2 ** max(0, recovery.level))
            lines.append(
                f"{DIM}{prefix}next entry ~{nxt:g} USDT "
                f"({recovery.multiplier:g}x · base {recovery.base_notional_usdt:g}){RESET}"
            )
    if live:
        lines.append(f"{DIM}refreshing · Ctrl+C to stop{RESET}")
    lines.append("")
    return "\n".join(lines)


def print_trades(
    symbol: str,
    *,
    limit: int | None,
    show_opens: bool,
    show_symbol_col: bool = False,
) -> int:
    text = _format_report(
        symbol,
        limit=limit,
        show_opens=show_opens,
        live=False,
        show_symbol_col=show_symbol_col,
    )
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0 if "No trades" not in text else 1


def follow_trades(
    symbol: str | None,
    *,
    limit: int | None,
    show_opens: bool,
    interval: float,
) -> int:
    """Clear and redraw until Ctrl+C.

    If ``symbol`` is None, re-resolve ``active_symbol()`` each tick (follow pick).
    """
    last = ""
    last_sym = ""
    follow_active = symbol is None
    try:
        while True:
            sym = (symbol or active_symbol() or "").upper()
            if not sym:
                text = f"{DIM}No active symbol yet · waiting… · Ctrl+C to stop{RESET}\n"
            else:
                if follow_active and sym != last_sym and last_sym:
                    # Force redraw banner when pick switches
                    last = ""
                text = _format_report(
                    sym,
                    limit=limit,
                    show_opens=show_opens,
                    live=True,
                    show_symbol_col=follow_active,
                )
                last_sym = sym
            if text != last:
                sys.stdout.write(CLEAR + text)
                sys.stdout.flush()
                last = text
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="All OB scalp trades + total PnL for a symbol")
    p.add_argument("symbol", nargs="?", help="Symbol (default: follow active stack)")
    p.add_argument("-n", "--limit", type=int, default=0,
                   help="Show only last N closes (0 = all)")
    p.add_argument("-f", "--follow", action="store_true",
                   help="Live refresh; without SYMBOL, follows active pick")
    p.add_argument("--interval", type=float, default=2.0,
                   help="Follow refresh seconds (default 2)")
    p.add_argument("--opens", action="store_true", help="Also show OPEN lines")
    p.add_argument("--list", action="store_true", help="List symbols with journals")
    args = p.parse_args()

    if args.list:
        found = sorted(
            d.name for d in LOG_ROOT.iterdir()
            if d.is_dir() and (d / "scalp_trades.log").exists()
        ) if LOG_ROOT.exists() else []
        for sym in found:
            closes = [r for r in parse_journal(sym) if r.kind != "OPEN"]
            total = sum(r.pnl or 0.0 for r in closes)
            mark = " *" if sym in running_symbols() else ""
            color = GREEN if total > 0 else RED if total < 0 else DIM
            print(f"{sym}{mark}  {color}{total:+.4f}{RESET} USDT  ({len(closes)} trades)")
        return 0

    pinned = (args.symbol or "").upper() or None
    limit = args.limit if args.limit and args.limit > 0 else None

    if args.follow:
        if pinned is None and not active_symbol():
            print(
                f"{DIM}No active symbol yet — will wait for pick…{RESET}",
                file=sys.stderr,
            )
        return follow_trades(
            pinned,
            limit=limit,
            show_opens=args.opens,
            interval=args.interval,
        )

    sym = pinned or (active_symbol() or "").upper()
    if not sym:
        print(f"{RED}No active symbol — pass one: ./obscalp-trades PYTHUSDT{RESET}", file=sys.stderr)
        return 1
    # Explicit SYMBOL → no Symbol column; one-shot of active → show column
    return print_trades(
        sym,
        limit=limit,
        show_opens=args.opens,
        show_symbol_col=pinned is None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
