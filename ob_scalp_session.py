"""Unified session log for OB scalp (all channels → one file)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"

# ANSI (stripped when writing to file)
_TAG_COLORS = {
    "BOT": "\033[36m",
    "EMA": "\033[35m",
    "TRADE": "\033[33m",
    "ML": "\033[34m",
    "AUTO": "\033[32m",
    "PNL": "\033[33m",
    "WARN": "\033[31m",
    "INFO": "\033[2m",
}
_RESET = "\033[0m"


def session_path(symbol: str) -> Path:
    path = LOG_ROOT / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path / "scalp_session.log"


def session_log(
    symbol: str,
    tag: str,
    message: str,
    *,
    also_print: bool = False,
) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = tag.upper()
    line = f"{ts} [{tag}] {message}"
    with open(session_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if also_print:
        color = _TAG_COLORS.get(tag, "")
        print(f"{color}{line}{_RESET}", flush=True)


def format_console(tag: str, message: str, ts: str | None = None) -> str:
    ts = ts or time.strftime("%Y-%m-%d %H:%M:%S")
    tag = tag.upper()
    color = _TAG_COLORS.get(tag, "")
    return f"{color}{ts} [{tag}] {message}{_RESET}"


def strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", text)
