#!/usr/bin/env python3
"""Pick OB scalp symbol via fscan-style scan and optionally launch the full stack.

Sits in front of the existing scalp bot — does not replace manual runs::

    obscalp ZECUSDT --execute
    ./obscalp-autotune ZECUSDT --execute

Examples:
    ./obscalp-pick                    # scan + show top picks
    ./obscalp-pick -y                 # pick best + start stack (autotune/watch/bot)
    ./obscalp-pick -y --keep-rank 3   # keep current symbol if still in top 3
    ./obscalp-pick -y --symbol SOLUSDT
    ./obscalp-pick --daemon --at 08:00,14:00,20:00 -y
    ./obscalp-pick --daemon -y --idle-min 90          # also rescan if no signals 90m
    ./obscalp-pick --daemon -y --at '' --idle-min 60  # idle-only rotation
    ./obscalp-pick --daemon -y --no-on-start          # schedule/idle only (skip boot pick)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

from futures_scan import BOLD, CYAN, DIM, GREEN, RESET, YELLOW, FAPI_BASE, TickerRow, trading_usdt_perps, fetch_tickers
from ob_scalp_activity import activity_summary, idle_minutes
from ob_scalp_stack import active_symbol, load_active, running_symbols, switch_stack
from orderbook_dca_grid import load_env_file

from trade_pick import (
    Candidate,
    enrich_scalp_candidates,
    fallback_pick_scalp,
    print_ranking_scalp,
    scalp_candidate_payload,
)

ROOT = Path(__file__).resolve().parent
PICK_LOG = ROOT / ".run" / "logs" / "scalp_picks.jsonl"


def collect_fscan_universe(
    base: str,
    *,
    min_volume: float,
    pool_size: int,
) -> list[TickerRow]:
    """fscan-style pool: volatile + movers + hot volume (deduped)."""
    allowed = trading_usdt_perps(base)
    tickers = fetch_tickers(base, allowed)
    liquid = [t for t in tickers if t.quote_volume >= min_volume]

    volatile = sorted(liquid, key=lambda t: t.range_pct, reverse=True)[:pool_size]
    movers = sorted(liquid, key=lambda t: abs(t.change_pct), reverse=True)[:pool_size]
    hots = sorted(liquid, key=lambda t: t.quote_volume, reverse=True)[: max(pool_size // 2, 10)]
    gainers = sorted(liquid, key=lambda t: t.change_pct, reverse=True)[: pool_size // 3]
    losers = sorted(liquid, key=lambda t: t.change_pct)[: pool_size // 3]

    by_sym: dict[str, TickerRow] = {}
    for bucket in (volatile, movers, hots, gainers, losers):
        for row in bucket:
            by_sym.setdefault(row.symbol, row)
    return list(by_sym.values())


def append_pick_record(record: dict[str, Any]) -> None:
    PICK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PICK_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def resolve_pick(
    candidates: list[Candidate],
    *,
    manual: str | None,
    keep_rank: int,
    current: str | None,
    idle_switch: bool = False,
) -> dict[str, Any]:
    if manual:
        sym = manual.upper()
        match = next((c for c in candidates if c.ticker.symbol == sym), None)
        if match:
            rank = candidates.index(match) + 1
            pick = {
                "symbol": sym,
                "confidence": min(0.9, 0.5 + match.local_score / 100),
                "reason": f"Manual {sym} — rank #{rank}, score {match.local_score:.1f}",
            }
        else:
            pick = {"symbol": sym, "confidence": 0.5, "reason": f"Manual override {sym} (not in scan pool)"}
        return pick

    if idle_switch and current:
        for c in candidates:
            if c.ticker.symbol != current:
                rank = candidates.index(c) + 1
                return {
                    "symbol": c.ticker.symbol,
                    "confidence": min(0.9, 0.5 + c.local_score / 100),
                    "reason": (
                        f"Idle switch from {current} → {c.ticker.symbol} "
                        f"(#{rank}, score {c.local_score:.1f}) — lateral / no activity"
                    ),
                }

    if keep_rank > 0 and current and not idle_switch:
        for i, c in enumerate(candidates):
            if c.ticker.symbol == current and i < keep_rank:
                return {
                    "symbol": current,
                    "confidence": min(0.9, 0.5 + c.local_score / 100),
                    "reason": f"Keeping {current} — still #{i + 1} in scan (top {keep_rank})",
                }

    return fallback_pick_scalp(candidates)


def run_scan(args: argparse.Namespace, *, idle_switch: bool = False) -> tuple[list[Candidate], dict[str, Any]]:
    base = args.base.rstrip("/")
    universe = collect_fscan_universe(
        base, min_volume=args.min_volume, pool_size=args.pool,
    )
    universe = [
        t for t in universe
        if args.min_range <= t.range_pct <= args.max_range
    ]
    if not universe:
        raise RuntimeError(
            f"No candidates (vol≥{args.min_volume / 1e6:.0f}M, "
            f"range {args.min_range:g}-{args.max_range:g}%)"
        )

    candidates = enrich_scalp_candidates(base, universe, max_analyze=args.analyze)
    if not candidates:
        raise RuntimeError("No candidates after OB analysis")

    current = active_symbol()
    pick = resolve_pick(
        candidates,
        manual=args.symbol.upper() if args.symbol else None,
        keep_rank=args.keep_rank,
        current=current,
        idle_switch=idle_switch,
    )
    return candidates, pick


def print_pick(pick: dict[str, Any], *, executed: bool = False) -> None:
    sym = pick.get("symbol", "").upper()
    print(f"\n{BOLD}{GREEN}▶ Scalp pick{RESET}")
    print(f"  {BOLD}Symbol{RESET}     {CYAN}{sym}{RESET}")
    print(f"  {BOLD}Confidence{RESET} {pick.get('confidence', '?')}")
    print(f"  {BOLD}Reason{RESET}     {pick.get('reason', '')}")
    if executed:
        print(f"\n  {DIM}Logs:{RESET}  tail -f .run/logs/{sym}/scalp_session.log")
    else:
        print(f"\n  {DIM}Launch:{RESET}  ./obscalp-pick -y")
        print(f"  {DIM}Manual:{RESET}  ./obscalp-pick -y --symbol {sym}")


def run_once(args: argparse.Namespace, *, idle_switch: bool = False, trigger: str = "") -> int:
    load_env_file(None)
    try:
        candidates, pick = run_scan(args, idle_switch=idle_switch)
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"{YELLOW}Pick failed: {exc}{RESET}", file=sys.stderr)
        return 1

    if trigger and not pick.get("reason", "").startswith("Idle"):
        pick = {**pick, "reason": f"{trigger} · {pick.get('reason', '')}"}

    if not args.json:
        print(f"{BOLD}fscan scalp pool · {len(candidates)} analyzed{RESET}")
        if trigger:
            print(f"{CYAN}{trigger}{RESET}")
        if running_symbols():
            print(f"{DIM}Running stacks: {', '.join(running_symbols())}{RESET}")
        if active_symbol():
            print(f"{DIM}Active symbol: {active_symbol()}{RESET}")
        print_ranking_scalp(candidates, limit=args.show)

    sym = pick["symbol"].upper()
    executed = False
    pids: dict[str, Any] = {}

    if args.execute:
        if sym != active_symbol() or sym not in running_symbols():
            pids = switch_stack(
                sym,
                execute=True,
                meta={
                    "pick_reason": pick.get("reason", ""),
                    "pick_confidence": pick.get("confidence"),
                    "pick_score": next(
                        (c.local_score for c in candidates if c.ticker.symbol == sym),
                        None,
                    ),
                },
            )
            executed = True
            print(f"\n{GREEN}Stack started on {sym}{RESET}  autotune={pids.get('autotune_pid')} watch={pids.get('watch_pid')}")
            drained = str(pids.get("drained") or "")
            stopped = str(pids.get("stopped") or "")
            if drained:
                print(f"{YELLOW}Draining (exits only): {drained}{RESET}")
            if stopped:
                print(f"{DIM}Stopped: {stopped}{RESET}")
        else:
            print(f"\n{DIM}Already running on {sym} — no switch{RESET}")

    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": sym,
        "executed": executed,
        "pick": pick,
        "top": [scalp_candidate_payload(c) for c in candidates[:5]],
        "pids": pids,
        "trigger": trigger or ("idle" if idle_switch else "manual" if args.symbol else "scheduled"),
    }
    append_pick_record(record)

    if args.json:
        print(json.dumps(record, indent=2))
    elif not args.quiet:
        print_pick(pick, executed=executed)

    return 0


def _parse_at_times(raw: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        out.append((int(hh), int(mm or "0")))
    return sorted(set(out))


def maybe_idle_rescan(args: argparse.Namespace, *, last_idle_pick_at: float) -> float:
    """Return updated last_idle_pick_at. Runs idle pick when activity is stale."""
    if args.idle_min <= 0:
        return last_idle_pick_at
    sym = active_symbol()
    if not sym or sym not in running_symbols():
        return last_idle_pick_at

    idle = idle_minutes(sym, mode=args.idle_mode)
    if idle < args.idle_min:
        return last_idle_pick_at

    cooldown_sec = max(60.0, args.idle_cooldown_min * 60.0)
    if last_idle_pick_at and (time.time() - last_idle_pick_at) < cooldown_sec:
        return last_idle_pick_at

    summary = activity_summary(sym)
    trigger = (
        f"Idle rescan — {sym} no {args.idle_mode} for {idle:.0f}m "
        f"(signal {summary['idle_signal_min']:.0f}m · trade {summary['idle_trade_min']:.0f}m)"
    )
    print(f"\n{CYAN}{trigger}{RESET}")
    run_once(args, idle_switch=True, trigger=trigger)
    return time.time()


def run_daemon(args: argparse.Namespace) -> int:
    times = _parse_at_times(args.at)
    if not times and args.idle_min <= 0:
        print("No valid --at times and --idle-min off", file=sys.stderr)
        return 1

    load_env_file(None)
    if not args.execute:
        print(f"{YELLOW}Daemon mode requires -y / --execute to launch stacks.{RESET}", file=sys.stderr)
        return 1

    sched = ", ".join(f"{h:02d}:{m:02d}" for h, m in times) if times else "off"
    idle_note = f" · idle rescan after {args.idle_min:g}m ({args.idle_mode})" if args.idle_min > 0 else ""
    on_start_note = " · pick on start" if args.on_start else ""
    print(f"{BOLD}Scalp pick daemon{RESET} — schedule {sched}{idle_note}{on_start_note}")
    print(f"{DIM}Ctrl+C to stop · log {PICK_LOG}{RESET}\n")

    last_run: dict[str, str] = {}
    last_idle_pick_at = 0.0

    if args.on_start:
        print(f"{CYAN}Startup pick{RESET}")
        run_once(args, trigger="Startup pick")
        last_idle_pick_at = time.time()

    try:
        while True:
            now = datetime.now()
            key = now.strftime("%Y-%m-%d")
            for h, m in times:
                slot = f"{key} {h:02d}:{m:02d}"
                if now.hour == h and now.minute == m and last_run.get(slot) != slot:
                    print(f"\n{CYAN}Scheduled pick @ {slot}{RESET}")
                    run_once(args, trigger=f"Scheduled pick @ {slot}")
                    last_run[slot] = slot

            last_idle_pick_at = maybe_idle_rescan(args, last_idle_pick_at=last_idle_pick_at)
            time.sleep(max(15.0, args.poll_sec))
    except KeyboardInterrupt:
        print(f"\n{DIM}Pick daemon stopped.{RESET}")
        return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="fscan-based OB scalp symbol picker (front layer for autotune stack)",
    )
    p.add_argument("--symbol", help="Skip scan ranking; use this symbol")
    p.add_argument("--min-volume", type=float, default=10_000_000, metavar="USDT",
                   help="Min 24h quote volume (default: 10M)")
    p.add_argument("--min-range", type=float, default=2.0, help="Min 24h range %% (default: 2)")
    p.add_argument("--max-range", type=float, default=28.0, help="Max 24h range %% (default: 28)")
    p.add_argument("--pool", type=int, default=30, help="fscan pool size per section")
    p.add_argument("--analyze", type=int, default=12, help="Deep OB score on top N from pool")
    p.add_argument("--show", type=int, default=8, help="Rows to print in ranking")
    p.add_argument("--keep-rank", type=int, default=3,
                   help="If active symbol still in top N, keep it (default: 3, 0=always switch)")
    p.add_argument("-y", "--execute", action="store_true",
                   help="Start full stack on pick (autotune + watch + bot)")
    p.add_argument("--daemon", action="store_true",
                   help="Run scheduled picks (use with --at)")
    p.add_argument("--at", default="08:00,14:00,20:00",
                   help="Daily pick times HH:MM (empty = idle-only daemon)")
    p.add_argument("--on-start", action=argparse.BooleanOptionalAction, default=True,
                   help="Run one pick when daemon starts (default: true; use --no-on-start to skip)")
    p.add_argument("--idle-min", type=float, default=90.0,
                   help="Rescan when no activity for N minutes (0=off, default 90)")
    p.add_argument("--idle-mode", choices=("signal", "trade", "entry"), default="signal",
                   help="What counts as activity for idle rescan (default: signal=OB long/short bar)")
    p.add_argument("--idle-cooldown-min", type=float, default=60.0,
                   help="Min minutes between idle rescans (default 60)")
    p.add_argument("--poll-sec", type=float, default=30.0,
                   help="Daemon poll interval seconds (default 30)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="Less output (still logs pick record)")
    p.add_argument("--base", default=FAPI_BASE)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.daemon:
        raise SystemExit(run_daemon(args))
    raise SystemExit(run_once(args))


if __name__ == "__main__":
    main()
