#!/usr/bin/env python3
"""Show OB scalp trade history from scalp_trades.log.

Usage:
    ./obscalp-trades              # active pool — all closes + total PnL
    ./obscalp-trades PYTHUSDT
    ./obscalp-trades -f           # live: all pool symbols (not only primary)
    ./obscalp-trades -f -n 15     # live, last 15 closes across pool
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
from ob_scalp_stack import active_symbol, load_active, running_symbols
from ob_scalp_adaptive import load_adaptive, load_trade_samples
from ob_scalp_dataset import load_bars
from ob_scalp_ml import load_tuned

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
_BLOCK_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"BLOCK (LONG|SHORT) trigger=(\S+)"
    r"(?: reason=(\S+))?"
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
    kind: str  # OPEN | BLOCK | close reason
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
    symbol: str = ""


def pool_symbols() -> list[str]:
    """Symbols to follow: pick pool if set, else primary / running."""
    data = load_active()
    pool = data.get("pool") or []
    out: list[str] = []
    for s in pool:
        u = str(s).upper().strip()
        if u and u not in out:
            out.append(u)
    if out:
        return out
    primary = (data.get("symbol") or active_symbol() or "").upper().strip()
    if primary:
        return [primary]
    return [s.upper() for s in running_symbols()]


def parse_journal(symbol: str) -> list[TradeRow]:
    path = journal_path(symbol)
    if not path.exists():
        return []
    sym = symbol.upper()
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
                    symbol=sym,
                )
            )
            continue
        m = _BLOCK_RE.match(line)
        if m:
            rows.append(
                TradeRow(
                    ts=m.group(1),
                    kind="BLOCK",
                    side=m.group(2),
                    qty=0.0,
                    notional=None,
                    entry=None,
                    exit=None,
                    gross_pct=None,
                    pnl=None,
                    level=0,
                    mult="",
                    outcome=(m.group(4) or "tag-block").strip(),
                    trigger=(m.group(3) or "").strip(),
                    symbol=sym,
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
                    symbol=sym,
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
    if kind in ("FLIP", "MAXBARS", "BLOCK"):
        return YELLOW
    return CYAN


def _pad_trunc(text: str, width: int) -> str:
    """Fixed-width cell: truncate with ellipsis if longer than width."""
    s = text or ""
    if width <= 0:
        return ""
    if len(s) > width:
        s = s[: max(0, width - 1)] + "…"
    return f"{s:<{width}}"


def _format_report(
    symbols: str | list[str],
    *,
    limit: int | None,
    show_opens: bool,
    live: bool = False,
    show_symbol_col: bool = False,
) -> str:
    if isinstance(symbols, str):
        syms = [symbols.upper()]
    else:
        syms = [s.upper() for s in symbols if s]
    if not syms:
        msg = f"{DIM}No symbols to follow · waiting… · Ctrl+C to stop{RESET}\n"
        return msg

    all_rows: list[TradeRow] = []
    for s in syms:
        all_rows.extend(parse_journal(s))
    all_rows.sort(key=lambda r: (r.ts, r.symbol, r.kind))

    closes = [r for r in all_rows if r.kind not in ("OPEN", "BLOCK")]
    if show_opens:
        rows = all_rows
    else:
        rows = [r for r in all_rows if r.kind not in ("OPEN",)]
    if limit is not None and limit > 0:
        rows = rows[-limit:]

    multi = len(syms) > 1 or show_symbol_col
    label = "+".join(syms) if len(syms) <= 3 else f"{len(syms)} symbols"
    lines: list[str] = []
    if not closes and not rows:
        lines.append(f"{DIM}No trades in journal for {label}{RESET}")
        for s in syms:
            lines.append(f"  {DIM}{journal_path(s)}{RESET}")
        if live:
            lines.append(f"\n{DIM}live · waiting for trades · Ctrl+C to stop{RESET}")
        return "\n".join(lines) + "\n"

    total_pnl = sum(r.pnl or 0.0 for r in closes)
    wins = sum(1 for r in closes if (r.pnl or 0) > 0)
    losses = sum(1 for r in closes if (r.pnl or 0) <= 0)
    t_color = GREEN if total_pnl > 0 else RED if total_pnl < 0 else DIM
    now = time.strftime("%H:%M:%S")

    head = f"{BOLD}{CYAN}{label}{RESET}  {DIM}{len(closes)} closed trades{RESET}"
    if live:
        head += f"  {DIM}live {now}{RESET}"
        if multi:
            head += f"  {DIM}(pool){RESET}"
    lines.append(head)
    lines.append(
        f"{BOLD}Total PnL  {t_color}{total_pnl:+.4f} USDT{RESET}  "
        f"{DIM}({wins}W/{losses}L){RESET}"
    )
    lines.append("")

    TRIG_W = 64
    if multi:
        hdr = (
            f"{'When':<19} {'Symbol':<14} {'Evt':<7} {'Side':<5} {'Vol USDT':>9} "
            f"{'Gross':>8} {'PnL USDT':>10}  {'Trigger':<{TRIG_W}} Note"
        )
        sep = "-" * (112 + (TRIG_W - 28))
    else:
        hdr = (
            f"{'When':<19} {'Evt':<7} {'Side':<5} {'Vol USDT':>9} "
            f"{'Gross':>8} {'PnL USDT':>10}  {'Trigger':<{TRIG_W}} Note"
        )
        sep = "-" * (96 + (TRIG_W - 28))
    lines.append(f"{DIM}{hdr}{RESET}")
    lines.append(f"{DIM}{sep}{RESET}")

    for r in rows:
        vol = r.notional
        if vol is None and r.entry is not None and r.qty > 0:
            vol = r.entry * r.qty
        vol_s = f"{vol:.2f}" if vol is not None else ""
        trig = (r.trigger or "—")[:TRIG_W]
        row_sym = r.symbol or syms[0]
        sym_cell = f"{BOLD}{row_sym:<14}{RESET} " if multi else ""
        if r.kind == "OPEN":
            note = f"qty {r.qty:g} · level {r.level} ({r.mult})"
            lines.append(
                f"{r.ts} {sym_cell}{_reason_color('OPEN')}{'OPEN':<7}{RESET} {r.side:<5} "
                f"{vol_s:>9} {'':>8} {'':>10}  {CYAN}{trig:<{TRIG_W}}{RESET} {DIM}{note}{RESET}"
            )
            continue
        if r.kind == "BLOCK":
            note = r.outcome or "combo blocked"
            lines.append(
                f"{r.ts} {sym_cell}{_reason_color('BLOCK')}{'BLOCK':<7}{RESET} {r.side:<5} "
                f"{'':>9} {'':>8} {'':>10}  {YELLOW}{trig:<{TRIG_W}}{RESET} {DIM}{note}{RESET}"
            )
            continue
        pnl_s = f"{r.pnl:+.4f}" if r.pnl is not None else ""
        gross_s = f"{r.gross_pct:+.3f}%" if r.gross_pct is not None else ""
        note = r.outcome or f"level {r.level}"
        lines.append(
            f"{r.ts} {sym_cell}{_reason_color(r.kind)}{r.kind:<7}{RESET} {r.side:<5} "
            f"{vol_s:>9} {gross_s:>8} {_pnl_color(r.pnl)}{pnl_s:>10}{RESET}  "
            f"{CYAN}{trig:<{TRIG_W}}{RESET} {DIM}{note}{RESET}"
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
        tag_w = min(56, max(28, max((len(t) for t in by_tag), default=28)))
        part_w = min(32, max(20, max((len(t) for t in by_part), default=20)))
        wl_w = 12  # e.g. "13W/4L"
        lines.append(f"\n{BOLD}By trigger tag{RESET}")
        for tag, pnls in sorted(by_tag.items(), key=lambda x: sum(x[1]), reverse=True):
            s = sum(pnls)
            w = sum(1 for p in pnls if p > 0)
            l = len(pnls) - w
            c = GREEN if s > 0 else RED if s < 0 else DIM
            wl = f"{w}W/{l}L"
            lines.append(
                f"  {_pad_trunc(tag, tag_w)} {c}{s:+10.4f}{RESET}  {DIM}{wl:<{wl_w}} · {len(pnls)}{RESET}"
            )
        lines.append(f"\n{BOLD}By trigger component{RESET}  {DIM}(credit each part of a combo){RESET}")
        for part, pnls in sorted(by_part.items(), key=lambda x: sum(x[1]), reverse=True):
            s = sum(pnls)
            w = sum(1 for p in pnls if p > 0)
            l = len(pnls) - w
            c = GREEN if s > 0 else RED if s < 0 else DIM
            wl = f"{w}W/{l}L"
            lines.append(
                f"  {_pad_trunc(part, part_w)} {c}{s:+10.4f}{RESET}  {DIM}{wl:<{wl_w}} · {len(pnls)}{RESET}"
            )

    # ML / learning snapshot (per symbol)
    lines.append(f"\n{BOLD}ML learning{RESET}")
    lines.append(
        f"{DIM}  {'Symbol':<14} {'Bars':>5} {'Samp':>5} {'CV L':>6} {'CV S':>6} "
        f"{'ml≥':>5} {'BT WR':>6} {'BT n':>5}  Adaptive{RESET}"
    )
    for s in syms:
        bars_n = len(load_bars(s))
        samp_n = len(load_trade_samples(s, limit=5000))
        tuned, meta = load_tuned(s)
        ml = meta.get("ml") or {}
        stats = meta.get("stats") or {}
        cv_l = ml.get("cv_long")
        cv_s = ml.get("cv_short")
        cv_l_s = f"{float(cv_l):.2f}" if cv_l is not None else "—"
        cv_s_s = f"{float(cv_s):.2f}" if cv_s is not None else "—"
        ml_floor = tuned.ml_min_prob if tuned else None
        ml_s = f"{ml_floor:.2f}" if ml_floor is not None else "—"
        bt_wr = stats.get("win_rate")
        bt_n = stats.get("trades")
        bt_wr_s = f"{float(bt_wr)*100:.0f}%" if bt_wr is not None else "—"
        bt_n_s = f"{int(float(bt_n))}" if bt_n is not None else "—"
        try:
            ada = load_adaptive(s)
            ada_note = (
                f"ml≥{ada.ml_min_prob:.2f} {ada.wins}W/{ada.losses}L "
                f"({ada.trades} live)"
            )
        except Exception:
            ada_note = "—"
        lines.append(
            f"  {s:<14} {bars_n:>5} {samp_n:>5} {cv_l_s:>6} {cv_s_s:>6} "
            f"{ml_s:>5} {bt_wr_s:>6} {bt_n_s:>5}  {DIM}{ada_note}{RESET}"
        )

    try:
        from ob_trig_learn import format_disabled_summary, load_disabled, refresh_trig_disabled

        refresh_trig_disabled()
        disabled = load_disabled()
        lines.append(
            f"\n{BOLD}Trig auto-disable{RESET}  {DIM}(n≥15 · pnl<0 · wr≤45%){RESET}"
        )
        if disabled:
            for name, info in sorted(disabled.items(), key=lambda x: float(x[1].get("pnl", 0))):
                lines.append(
                    f"  {name:<28} {RED}{float(info.get('pnl', 0)):+.4f}{RESET}  "
                    f"{DIM}{info.get('wins', 0)}W/{info.get('losses', 0)}L · "
                    f"n={info.get('n', 0)} · OFF{RESET}"
                )
        else:
            lines.append(f"  {DIM}{format_disabled_summary(disabled)}{RESET}")
    except Exception as exc:
        lines.append(f"\n{DIM}Trig auto-disable unavailable: {exc}{RESET}")

    try:
        from ob_trig_learn import blocked_tag_map, tag_max_losses

        max_l = tag_max_losses()
        blocked = blocked_tag_map(force=True)
        lines.append(
            f"\n{BOLD}Trig combo block{RESET}  {DIM}(exact tag · ≥{max_l}L){RESET}"
        )
        if blocked:
            tag_w = 48
            for tag, info in sorted(
                blocked.items(),
                key=lambda x: (int(x[1].get("losses", 0)), float(x[1].get("pnl", 0))),
                reverse=True,
            )[:20]:
                pnl = float(info.get("pnl", 0))
                c = GREEN if pnl > 0 else RED if pnl < 0 else DIM
                wl = f"{info.get('wins', 0)}W/{info.get('losses', 0)}L"
                lines.append(
                    f"  {_pad_trunc(tag, tag_w)} {c}{pnl:+10.4f}{RESET}  "
                    f"{DIM}{wl:<12} · BLOCK{RESET}"
                )
            if len(blocked) > 20:
                lines.append(f"  {DIM}… +{len(blocked) - 20} more{RESET}")
        else:
            lines.append(f"  {DIM}none{RESET}")
    except Exception as exc:
        lines.append(f"\n{DIM}Trig combo block unavailable: {exc}{RESET}")

    for s in syms:
        recovery = load_state(s)
        if recovery.level > 0 or recovery.cumulative_loss_usdt > 0:
            prefix = f"{s}  " if multi else ""
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
    symbols: str | list[str],
    *,
    limit: int | None,
    show_opens: bool,
    show_symbol_col: bool = False,
) -> int:
    text = _format_report(
        symbols,
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

    If ``symbol`` is None, follow the whole pick pool (all journals merged).
    """
    last = ""
    last_key = ""
    follow_pool = symbol is None
    try:
        while True:
            if follow_pool:
                syms = pool_symbols()
            else:
                syms = [symbol.upper()] if symbol else []
            key = ",".join(syms)
            if not syms:
                text = f"{DIM}No active pool yet · waiting… · Ctrl+C to stop{RESET}\n"
            else:
                if key != last_key and last_key:
                    last = ""
                text = _format_report(
                    syms,
                    limit=limit,
                    show_opens=show_opens,
                    live=True,
                    show_symbol_col=follow_pool or len(syms) > 1,
                )
                last_key = key
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
    p.add_argument("symbol", nargs="?", help="Symbol (default: follow active pool)")
    p.add_argument("-n", "--limit", type=int, default=0,
                   help="Show only last N closes (0 = all)")
    p.add_argument("-f", "--follow", action="store_true",
                   help="Live refresh; without SYMBOL, follows whole pick pool")
    p.add_argument("--interval", type=float, default=2.0,
                   help="Follow refresh seconds (default 2)")
    p.add_argument("--opens", action=argparse.BooleanOptionalAction, default=True,
                   help="Include OPEN rows in the table (default on; use --no-opens to hide)")
    p.add_argument("--list", action="store_true", help="List symbols with journals")
    args = p.parse_args()

    if args.list:
        found = sorted(
            d.name for d in LOG_ROOT.iterdir()
            if d.is_dir() and (d / "scalp_trades.log").exists()
        ) if LOG_ROOT.exists() else []
        for sym in found:
            closes = [r for r in parse_journal(sym) if r.kind not in ("OPEN", "BLOCK")]
            total = sum(r.pnl or 0.0 for r in closes)
            mark = " *" if sym in running_symbols() else ""
            color = GREEN if total > 0 else RED if total < 0 else DIM
            print(f"{sym}{mark}  {color}{total:+.4f}{RESET} USDT  ({len(closes)} trades)")
        return 0

    pinned = (args.symbol or "").upper() or None
    limit = args.limit if args.limit and args.limit > 0 else None

    if args.follow:
        if pinned is None and not pool_symbols():
            print(
                f"{DIM}No active pool yet — will wait for pick…{RESET}",
                file=sys.stderr,
            )
        return follow_trades(
            pinned,
            limit=limit,
            show_opens=args.opens,
            interval=args.interval,
        )

    if pinned:
        return print_trades(
            pinned,
            limit=limit,
            show_opens=args.opens,
            show_symbol_col=False,
        )

    syms = pool_symbols()
    if not syms:
        print(f"{RED}No active pool — pass a symbol: ./obscalp-trades PYTHUSDT{RESET}", file=sys.stderr)
        return 1
    return print_trades(
        syms,
        limit=limit,
        show_opens=args.opens,
        show_symbol_col=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
