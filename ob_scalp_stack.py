"""Start/stop the full OB scalp stack: autotune + watch + bot."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ob_scalp_autotune import autotune_pid_path, read_pid, stop_bot
from ob_scalp_watch import watch_pid_path
from trade_sounds import play_sound

ROOT = Path(__file__).resolve().parent
ACTIVE_PATH = ROOT / ".run" / "scalp_active.json"
LOG_ROOT = ROOT / ".run" / "logs"


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


def _kill_pid_file(path: Path, needle: str) -> None:
    if not path.exists():
        return
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return
    if _pid_alive(pid, needle):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.0)
        except OSError:
            pass
    path.unlink(missing_ok=True)


def stop_watch(symbol: str) -> None:
    _kill_pid_file(watch_pid_path(symbol.upper()), "ob_scalp_watch.py")


def stop_autotune(symbol: str) -> None:
    _kill_pid_file(autotune_pid_path(symbol.upper()), "ob_scalp_autotune.py")


def stop_stack(symbol: str) -> None:
    """Stop bot, autotune, and watch for one symbol."""
    sym = symbol.upper()
    stop_bot(sym)
    stop_autotune(sym)
    stop_watch(sym)


def running_symbols() -> list[str]:
    out: list[str] = []
    if not LOG_ROOT.exists():
        return out
    for sym_dir in LOG_ROOT.iterdir():
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        ap = autotune_pid_path(sym)
        if ap.exists():
            try:
                pid = int(ap.read_text().strip())
                if _pid_alive(pid, "ob_scalp_autotune.py"):
                    out.append(sym)
            except ValueError:
                continue
    return sorted(out)


def load_active() -> dict[str, Any]:
    if not ACTIVE_PATH.exists():
        return {}
    try:
        return json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_active(symbol: str, meta: dict[str, Any] | None = None) -> None:
    ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol.upper(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **(meta or {}),
    }
    ACTIVE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def active_symbol() -> str | None:
    data = load_active()
    sym = str(data.get("symbol", "") or "").upper()
    if sym and sym in running_symbols():
        return sym
    running = running_symbols()
    return running[0] if running else sym or None


def _python() -> str:
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def start_stack(
    symbol: str,
    *,
    execute: bool = True,
    interval_min: float = 2.0,
    min_bars: int = 12,
    min_delta: float = 0.01,
) -> dict[str, int | str]:
    """Launch autotune (manages bot) + unified log watch in background."""
    sym = symbol.upper()
    log_dir = LOG_ROOT / sym
    log_dir.mkdir(parents=True, exist_ok=True)

    py = _python()
    env = os.environ.copy()

    autotune_out = open(log_dir / "autotune_daemon.log", "a", encoding="utf-8")
    autotune_cmd = [
        py, "-u", str(ROOT / "ob_scalp_autotune.py"),
        sym,
        "--interval-min", str(interval_min),
        "--min-bars", str(min_bars),
        "--min-delta", str(min_delta),
        "--bar-sec", "60",
    ]
    if execute:
        autotune_cmd.append("--execute")

    autotune_proc = subprocess.Popen(
        autotune_cmd,
        cwd=ROOT,
        env=env,
        stdout=autotune_out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(1.5)

    watch_out = open(log_dir / "watch_daemon.log", "a", encoding="utf-8")
    watch_cmd = [sys.executable, "-u", str(ROOT / "ob_scalp_watch.py"), sym, "--daemon", "--interval", "1"]
    watch_proc = subprocess.Popen(
        watch_cmd,
        cwd=ROOT,
        env=env,
        stdout=watch_out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    bot_pid = read_pid(sym)
    return {
        "symbol": sym,
        "autotune_pid": autotune_proc.pid,
        "watch_pid": watch_proc.pid,
        "bot_pid": bot_pid or 0,
    }


def switch_stack(
    symbol: str,
    *,
    execute: bool = True,
    stop_others: bool = True,
    meta: dict[str, Any] | None = None,
) -> dict[str, int | str]:
    """Stop other scalp stacks and start the chosen symbol."""
    sym = symbol.upper()
    prev = active_symbol()
    to_stop: set[str] = set()
    if stop_others:
        to_stop.update(s for s in running_symbols() if s != sym)
        if prev and prev != sym:
            to_stop.add(prev)

    if to_stop:
        play_sound("cycle_end")
        for other in sorted(to_stop):
            stop_stack(other)

    play_sound("pick")
    pids = start_stack(sym, execute=execute)
    save_active(sym, meta)
    return pids
