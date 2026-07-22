#!/usr/bin/env python3
"""Unified follow log: symbol pick → session of active symbol until next pick.

Writes to .run/logs/scalp_follow.log and optional console.

Usage:
    ./obscalp-follow --console          # live in terminal
    tail -f .run/logs/scalp_follow.log  # same stream from file
    ./obscalp-follow --daemon           # background writer
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ob_scalp_session import format_console, session_path, strip_ansi
from ob_scalp_stack import ACTIVE_PATH, load_active
from ob_scalp_watch import TailState

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"
FOLLOW_LOG = LOG_ROOT / "scalp_follow.log"
PICK_LOG = LOG_ROOT / "scalp_picks.jsonl"
FOLLOW_PID = LOG_ROOT / "follow.pid"


def follow_pid_path() -> Path:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return FOLLOW_PID


def _pid_alive(pid: int, needle: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return needle in out


def ensure_single_follow() -> None:
    path = follow_pid_path()
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            if _pid_alive(pid, "ob_scalp_follow.py"):
                print(f"Follow already running pid={pid}", file=sys.stderr)
                print(f"tail -f {FOLLOW_LOG}", file=sys.stderr)
                sys.exit(0)
        except ValueError:
            pass
    path.write_text(str(os.getpid()), encoding="utf-8")


def write_follow(tag: str, message: str, *, console: bool) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = tag.upper()
    line = f"{ts} [{tag}] {message}"
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with open(FOLLOW_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if console:
        print(format_console(tag, message, ts=ts), flush=True)


def _pick_summary(record: dict[str, Any]) -> tuple[str, list[str]]:
    pick = record.get("pick") or {}
    sym = str(pick.get("symbol", "")).upper()
    lines = [
        "═" * 56,
        f"▶ SELECTED {sym}",
        f"  {pick.get('reason', '')}",
    ]
    conf = pick.get("confidence")
    if conf is not None:
        lines.append(f"  confidence {conf}")
    top = record.get("top") or []
    if top:
        parts = [f"{t.get('symbol', '?')}({t.get('scalp_score', '?')})" for t in top[:5]]
        lines.append(f"  ranking: {', '.join(parts)}")
    if record.get("executed"):
        lines.append("  stack started (autotune + watch + bot)")
    lines.append("═" * 56)
    return sym, lines


def emit_switch(
    symbol: str,
    *,
    record: dict[str, Any] | None,
    previous: str | None,
    console: bool,
) -> None:
    if previous and previous != symbol:
        write_follow("PICK", f"◀ end {previous}", console=console)
    if record:
        _, lines = _pick_summary(record)
        for line in lines:
            write_follow("PICK", line, console=console)
    else:
        meta = load_active()
        reason = meta.get("pick_reason", "active symbol")
        write_follow("PICK", "═" * 56, console=console)
        write_follow("PICK", f"▶ ACTIVE {symbol}" + (f" (was {previous})" if previous else ""), console=console)
        write_follow("PICK", f"  {reason}", console=console)
        write_follow("PICK", "═" * 56, console=console)


def load_pick_records(*, tail: int = 0) -> list[dict[str, Any]]:
    if not PICK_LOG.exists():
        return []
    lines = PICK_LOG.read_text(encoding="utf-8").strip().splitlines()
    if tail > 0:
        lines = lines[-tail:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def backfill_follow(*, console: bool) -> int:
    records = load_pick_records()
    if not records:
        active = load_active()
        sym = str(active.get("symbol", "") or "").upper()
        if sym:
            emit_switch(sym, record=None, previous=None, console=console)
        return 0

    n = 0
    prev: str | None = None
    for rec in records:
        sym, _ = _pick_summary(rec)
        if sym:
            emit_switch(sym, record=rec, previous=prev, console=console)
            prev = sym
            n += 1
    return n


def run_follow(*, console: bool, interval: float, backfill: bool) -> None:
    if backfill:
        backfill_follow(console=console)

    pick_tail = TailState(PICK_LOG)
    active_symbol: str | None = None
    session_tail: TailState | None = None

    # Start from current active symbol (session from end — only new lines)
    meta = load_active()
    sym = str(meta.get("symbol", "") or "").upper()
    if sym:
        active_symbol = sym
        session_tail = TailState(session_path(sym))
        if backfill:
            emit_switch(sym, record=None, previous=None, console=console)
        else:
            write_follow(
                "INFO",
                f"following {sym} — waiting for session lines (tail -f .run/logs/{sym}/scalp_session.log)",
                console=console,
            )

    try:
        while True:
            # New picks from jsonl
            for line in pick_tail.read_new():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sym, _ = _pick_summary(record)
                if not sym:
                    continue
                if sym != active_symbol:
                    emit_switch(sym, record=record, previous=active_symbol, console=console)
                    active_symbol = sym
                    session_tail = TailState(session_path(sym))

            # Active json changed without pick log (manual switch)
            meta = load_active()
            sym = str(meta.get("symbol", "") or "").upper()
            if sym and sym != active_symbol:
                emit_switch(sym, record=None, previous=active_symbol, console=console)
                active_symbol = sym
                session_tail = TailState(session_path(sym))

            # Forward session lines for active symbol
            if active_symbol and session_tail is not None:
                sp = session_path(active_symbol)
                if session_tail.path != sp:
                    session_tail = TailState(sp)
                for raw in session_tail.read_new():
                    plain = strip_ansi(raw).strip()
                    if not plain:
                        continue
                    write_follow("LIVE", plain, console=console)

            time.sleep(interval)
    except KeyboardInterrupt:
        write_follow("INFO", "follow stopped", console=console)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Follow log: pick events + live session of active symbol",
    )
    p.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds")
    p.add_argument("--console", action="store_true", help="Print to terminal")
    p.add_argument("--daemon", action="store_true", help="Background (no console)")
    p.add_argument("--backfill", action="store_true", help="Replay pick history into follow log")
    args = p.parse_args()

    if not args.backfill:
        ensure_single_follow()

    console = args.console and not args.daemon
    if args.daemon and not console:
        out = open(LOG_ROOT / "follow_daemon.log", "a", encoding="utf-8")
        sys.stdout = out
        sys.stderr = out

    run_follow(console=console, interval=args.interval, backfill=args.backfill)


if __name__ == "__main__":
    main()
