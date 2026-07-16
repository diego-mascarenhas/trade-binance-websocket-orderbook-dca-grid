#!/usr/bin/env python3
"""Merge all scalp logs into one live session log + optional console view."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ob_scalp_session import format_console, session_log, session_path, strip_ansi

ROOT = Path(__file__).resolve().parent

# source file → tag for unified log
_SOURCES = {
    "scalp_stdout.log": "BOT",
    "scalp_autotune.log": "AUTO",
    "scalp_trades.log": "TRADE",
    "scalp_learn.jsonl": "LEARN",
    "scalp_ema.log": "EMA",
}

_BAR_RE = re.compile(
    r"bar (\d{2}:\d{2}:\d{2}).*mid ([\d.]+).*imb ([\d.]+)%"
)
_EMA_INLINE = re.compile(r"EMA\d+/\d+")
_SIGNAL_RE = re.compile(r"→ (LONG|SHORT)|MARKET (LONG|SHORT)|EMA filter block|ML filter block|▶ MARKET|(TP|SL|FLIP|MAXBARS)")


def watch_pid_path(symbol: str) -> Path:
    p = ROOT / ".run/logs" / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "watch.pid"


def ensure_single_watch(symbol: str) -> None:
    path = watch_pid_path(symbol)
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            if _pid_alive(pid, "ob_scalp_watch.py"):
                print(f"Watch already running pid={pid}", file=sys.stderr)
                sys.exit(0)
        except ValueError:
            pass
    path.write_text(str(os.getpid()), encoding="utf-8")


def _pid_alive(pid: int, needle: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return needle in out


def _is_noise_line(msg: str) -> bool:
    low = msg.lower()
    return any(
        k in low
        for k in (
            "warnings.warn",
            "userwarning",
            "sklearn/utils/parallel",
            "site-packages/sklearn",
        )
    )


class TailState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        self.offset = size
        return chunk.splitlines()


def _summarize_bot_line(line: str) -> str | None:
    """Reduce noisy stdout to session-friendly lines."""
    plain = strip_ansi(line).strip()
    if not plain:
        return None
    if plain.startswith("OB scalp ·") or "Ctrl+C to stop" in plain:
        return plain.replace("\x1b", "")
    m = _BAR_RE.search(plain)
    if m:
        return f"bar {m.group(1)} mid={m.group(2)} imb={m.group(3)}%"
    if _EMA_INLINE.search(plain) or ("slope" in plain and "allow" in plain):
        return None  # EMA lines come from scalp_ema.log only
    if _SIGNAL_RE.search(plain):
        return plain
    if any(k in plain for k in ("flat on exchange", "cooldown", "Recovery locked", "Opposite hedge", "filter block")):
        return plain
    if "PnL " in plain or plain.startswith("PnL "):
        return plain
    if "session " in plain and "USDT" in plain and "total " in plain:
        return plain
    if " @ " in plain and "pnl " in plain and ("TP " in plain or "SL " in plain):
        return plain
    if plain.startswith("Learn ") or plain.startswith("Learn watch"):
        return plain
    return None


def _format_learn_line(plain: str) -> str | None:
    try:
        rec = json.loads(plain)
    except json.JSONDecodeError:
        return plain
    if not isinstance(rec, dict):
        return plain
    verdict = rec.get("verdict", "?")
    label = rec.get("label", "?")
    move = rec.get("best_move_pct", 0)
    reason = rec.get("reason", "")
    signal = rec.get("signal", "")
    pnl = rec.get("net_usdt", 0)
    human = {
        "premature_sl": "SL prematuro — señal prosperó",
        "signal_ok_sl_tight": "señal OK — SL ajustado",
        "sl_correct": "SL correcto",
        "tp_good": "TP acertado",
        "tp_early": "TP temprano",
        "tp_weak_follow": "TP OK — poco follow",
    }.get(str(verdict), str(verdict))
    return (
        f"{human} · {signal.upper()} after {reason} · move={move:+.3f}% "
        f"label={label} pnl={pnl:+.4f}"
    )


def merge_once(symbol: str, states: dict[str, TailState], *, console: bool) -> int:
    sym = symbol.upper()
    log_dir = ROOT / ".run/logs" / sym
    n = 0
    for fname, tag in _SOURCES.items():
        path = log_dir / fname
        if fname not in states:
            states[fname] = TailState(path)
        for raw in states[fname].read_new():
            plain = strip_ansi(raw).strip()
            if not plain:
                continue
            out_tag = tag
            if tag == "BOT":
                summary = _summarize_bot_line(raw)
                if not summary:
                    continue
                msg = strip_ansi(summary).strip()
                if msg.startswith("PnL "):
                    out_tag = "PNL"
                    msg = msg[4:].strip()
            elif tag == "LEARN":
                msg = _format_learn_line(plain)
                if not msg:
                    continue
            elif tag in ("TRADE", "EMA", "AUTO"):
                msg = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", "", plain)
                if _is_noise_line(msg):
                    continue
            else:
                msg = plain
            session_log(sym, out_tag, msg)
            if console:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                print(format_console(out_tag, msg, ts=ts), flush=True)
            n += 1
    return n


def backfill_session(symbol: str) -> int:
    """One-shot import of existing log tails into session file."""
    sym = symbol.upper()
    existing = set()
    sp = session_path(sym)
    if sp.exists():
        for line in sp.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            existing.add(line[-120:])
    states: dict[str, TailState] = {}
    for fname in _SOURCES:
        path = ROOT / ".run/logs" / sym / fname
        if path.exists():
            states[fname] = TailState(Path("/dev/null"))
            states[fname].offset = 0
            states[fname].path = path
    n = 0
    for fname, tag in _SOURCES.items():
        if fname not in states:
            continue
        for raw in states[fname].read_new():
            plain = strip_ansi(raw).strip()
            if not plain:
                continue
            if tag == "BOT":
                summary = _summarize_bot_line(raw)
                if not summary:
                    continue
                msg = summary
            elif tag == "LEARN":
                msg = _format_learn_line(plain) or plain
            else:
                msg = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", "", plain)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} [{tag}] {msg}"
            if line[-120:] in existing:
                continue
            session_log(sym, tag, msg)
            n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="Unified OB scalp session log")
    p.add_argument("symbol", nargs="?", default="ZECUSDT")
    p.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds")
    p.add_argument("--console", action="store_true", help="Also print to terminal")
    p.add_argument("--backfill", action="store_true", help="Import existing logs once")
    p.add_argument("--daemon", action="store_true", help="Background merge only (no console)")
    args = p.parse_args()

    sym = args.symbol.upper()
    if not args.backfill:
        ensure_single_watch(sym)
    if args.backfill:
        n = backfill_session(sym)
        print(f"Backfilled {n} lines → {session_path(sym)}", file=sys.stderr)

    states: dict[str, TailState] = {}
    console = args.console and not args.daemon
    session_log(sym, "INFO", f"session watch started interval={args.interval}s", also_print=console)

    try:
        while True:
            merge_once(sym, states, console=console)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        session_log(sym, "INFO", "session watch stopped", also_print=console)


if __name__ == "__main__":
    main()
