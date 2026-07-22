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
    clear_drain(sym)


def drain_path(symbol: str) -> Path:
    return LOG_ROOT / symbol.upper() / "scalp_drain"


def is_draining(symbol: str) -> bool:
    return drain_path(symbol).exists()


def arm_drain(symbol: str, *, reason: str = "") -> None:
    """Mark symbol to manage exits only (no new entries) until flat."""
    path = drain_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "armed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "symbol switch",
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def clear_drain(symbol: str) -> None:
    drain_path(symbol).unlink(missing_ok=True)


def bot_alive(symbol: str) -> bool:
    pid = read_pid(symbol)
    return bool(pid and _pid_alive(pid, "orderbook_ob_scalp.py"))


def autotune_alive(symbol: str) -> bool:
    path = autotune_pid_path(symbol.upper())
    if not path.exists():
        return False
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return False
    return _pid_alive(pid, "ob_scalp_autotune.py")


def symbol_has_open_position(symbol: str, *, recv: int = 15000) -> bool:
    """True if Binance futures has a non-zero position on symbol."""
    from orderbook_dca_grid import load_env_file, load_keys, _signed_request

    load_env_file(None)
    api, sec = load_keys(None)
    if not api or not sec:
        return False
    rows = _signed_request(
        "GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()}, api, sec, recv,
    )
    for row in rows or []:
        if abs(float(row.get("positionAmt") or 0)) > 0:
            return True
    return False


def begin_drain(symbol: str, *, reason: str = "") -> None:
    """Keep bot for TP/SL; stop autotune so it cannot reopen after flat."""
    sym = symbol.upper()
    arm_drain(sym, reason=reason)
    stop_autotune(sym)
    try:
        from ob_scalp_recovery import append_journal
        append_journal(sym, f"DRAIN armed — exits only ({reason or 'symbol switch'})")
    except Exception:
        pass


def running_symbols() -> list[str]:
    """Symbols with a live autotune and/or scalp bot (includes drain handoffs)."""
    out: set[str] = set()
    if not LOG_ROOT.exists():
        return []
    for sym_dir in LOG_ROOT.iterdir():
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        ap = autotune_pid_path(sym)
        if ap.exists():
            try:
                pid = int(ap.read_text().strip())
                if _pid_alive(pid, "ob_scalp_autotune.py"):
                    out.add(sym)
            except ValueError:
                pass
        if bot_alive(sym):
            out.add(sym)
    return sorted(out)


def draining_symbols() -> list[str]:
    return [s for s in running_symbols() if is_draining(s)]


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
    min_bars: int = 10,
    min_delta: float = 0.01,
) -> dict[str, int | str]:
    """Launch autotune (manages bot) + unified log watch in background."""
    sym = symbol.upper()
    log_dir = LOG_ROOT / sym
    log_dir.mkdir(parents=True, exist_ok=True)

    py = _python()
    env = os.environ.copy()

    autotune_out = open(log_dir / "autotune_daemon.log", "a", encoding="utf-8")
    # bootstrap-bars must match min-bars or empty symbols wait ~30 min (autotune default)
    autotune_cmd = [
        py, "-u", str(ROOT / "ob_scalp_autotune.py"),
        sym,
        "--interval-min", str(interval_min),
        "--min-bars", str(min_bars),
        "--bootstrap-bars", str(min_bars),
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
    """Start the chosen symbol. Other stacks: drain if open position, else stop."""
    return sync_stacks(
        [symbol],
        execute=execute,
        retire_others=stop_others,
        meta=meta,
    )


def _retire_symbol(other: str, *, reason: str) -> str:
    """Drain or stop one symbol. Returns 'drained' | 'stopped'."""
    if is_draining(other) and bot_alive(other):
        return "drained"
    has_pos = False
    try:
        has_pos = symbol_has_open_position(other)
    except Exception as exc:
        print(f"warn: position check {other} failed: {exc}", file=sys.stderr)
    if has_pos and bot_alive(other):
        begin_drain(other, reason=reason)
        return "drained"
    stop_stack(other)
    if has_pos and not bot_alive(other):
        print(
            f"warn: {other} had an open position but no live bot — "
            f"stopped stack; manage/close manually on Binance",
            file=sys.stderr,
        )
    return "stopped"


def sync_stacks(
    symbols: list[str],
    *,
    execute: bool = True,
    retire_others: bool = True,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep exactly these scalp stacks running (primary = first).

    - Missing → start_stack
    - Outside list → drain (open pos) or stop
    - Primary saved as active for follow / trades -f
    """
    wanted = [s.upper() for s in symbols if s]
    if not wanted:
        raise ValueError("sync_stacks requires at least one symbol")
    primary = wanted[0]
    wanted_set = set(wanted)

    drained: list[str] = []
    stopped: list[str] = []
    started: list[str] = []
    kept: list[str] = []

    if retire_others:
        to_retire = {s for s in running_symbols() if s not in wanted_set}
        # Also retire previous active if it somehow isn't running-listed
        prev = load_active().get("symbol", "")
        if prev and prev.upper() not in wanted_set:
            to_retire.add(prev.upper())
        if to_retire:
            play_sound("cycle_end")
            reason = f"sync → {','.join(wanted)}"
            for other in sorted(to_retire):
                result = _retire_symbol(other, reason=reason)
                if result == "drained":
                    drained.append(other)
                else:
                    stopped.append(other)

    play_sound("pick")
    for sym in wanted:
        if is_draining(sym):
            clear_drain(sym)
        if autotune_alive(sym):
            kept.append(sym)
            continue
        pids = start_stack(sym, execute=execute)
        started.append(sym)
        # brief stagger so Binance / disk aren't slammed
        time.sleep(0.4)
        _ = pids

    save_active(
        primary,
        {
            **(meta or {}),
            "pool": wanted,
            "pool_count": len(wanted),
        },
    )
    return {
        "symbol": primary,
        "pool": wanted,
        "started": started,
        "kept": kept,
        "drained": drained,
        "stopped": stopped,
        "drained_csv": ",".join(drained),
        "stopped_csv": ",".join(stopped),
        "started_csv": ",".join(started),
        "kept_csv": ",".join(kept),
    }
