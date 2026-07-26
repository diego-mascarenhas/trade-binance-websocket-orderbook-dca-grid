#!/usr/bin/env python3
"""Start/stop/status for per-symbol DCA supervisors (no position close on stop)."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run" / "pids"
LOG_DIR = ROOT / ".run" / "logs"
GRID_SCRIPT = ROOT / "orderbook_dca_grid.py"
FIB_SCRIPT = ROOT / "orderbook_micro_grid.py"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def parse_pairs(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        sym = part.strip().upper()
        if sym:
            out.append(sym)
    return out


def allowed_symbols() -> set[str] | None:
    """None = no whitelist."""
    raw = _env("FUTURES_PAIRS")
    if not raw:
        return None
    pairs = parse_pairs(raw)
    return set(pairs) if pairs else None


def detect_backend() -> str:
    mode = _env("BOTCTL_MODE", "auto").lower()
    if mode in ("systemd", "pidfile"):
        return mode
    if shutil.which("systemctl"):
        unit = Path(f"/etc/systemd/system/{_env('FUTURES_UNIT', 'dca-futures')}@.service")
        if unit.exists():
            return "systemd"
    return "pidfile"


def futures_unit_template() -> str:
    return _env("FUTURES_UNIT", "dca-futures")


def _systemctl(use_sudo: bool, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = (["sudo"] if use_sudo and os.geteuid() != 0 else []) + ["systemctl", *args]
    return subprocess.run(cmd, text=True, capture_output=True)


def _systemctl_read(*args: str) -> subprocess.CompletedProcess[str]:
    """Read-only systemctl: try without sudo first (telegram daemon has no TTY)."""
    last: subprocess.CompletedProcess[str] | None = None
    for use_sudo in (False, _use_sudo_systemctl()):
        if use_sudo and os.geteuid() == 0:
            continue
        proc = _systemctl(use_sudo, *args)
        last = proc
        if proc.returncode == 0:
            return proc
        err = (proc.stderr or "").lower()
        if "password" in err or "not allowed" in err or "permission denied" in err:
            continue
        if proc.stdout.strip():
            return proc
    assert last is not None
    return last


def _systemctl_write(*args: str) -> subprocess.CompletedProcess[str]:
    return _systemctl(_use_sudo_systemctl(), *args)


def _unit_templates() -> list[str]:
    """Primary FUTURES_UNIT plus legacy dca-super if still installed."""
    primary = futures_unit_template()
    templates = [primary]
    legacy = "dca-super"
    if legacy != primary and Path(f"/etc/systemd/system/{legacy}@.service").exists():
        templates.append(legacy)
    return templates


def _list_systemd_running(templates: list[str] | None = None) -> list[str]:
    templates = templates or _unit_templates()
    running: list[str] = []
    for tpl in templates:
        proc = _systemctl_read(
            "list-units", "--type=service", "--state=active", f"{tpl}@*", "--no-legend", "--plain",
        )
        prefix = f"{tpl}@"
        for line in proc.stdout.splitlines():
            unit = line.split()[0] if line.split() else ""
            if unit.startswith(prefix) and unit.endswith(".service"):
                running.append(unit[len(prefix):-len(".service")].upper())
    return sorted(set(running))


def _pgrep_pids(symbol: str) -> list[int]:
    """PIDs of supervisor scripts for this symbol (grid and legacy staged)."""
    sym = symbol.upper()
    pids: list[int] = []
    for pattern in (f"orderbook_dca_grid.py {sym}", f"orderbook_staged_exit.py {sym}"):
        try:
            proc = subprocess.run(["pgrep", "-f", pattern], text=True, capture_output=True)
        except FileNotFoundError:
            continue
        for line in proc.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
    return sorted(set(pids))


def _pgrep_fib_pids(symbol: str) -> list[int]:
    """PIDs of fib / micro-grid bots for this symbol."""
    sym = symbol.upper()
    pids: list[int] = []
    for pattern in (f"orderbook_micro_grid.py {sym}",):
        try:
            proc = subprocess.run(["pgrep", "-f", pattern], text=True, capture_output=True)
        except FileNotFoundError:
            continue
        for line in proc.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
    return sorted(set(pids))


def _fib_pid_path(symbol: str) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR / f"fib-{symbol.upper()}.pid"


def _fib_log_path(symbol: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"fib-{symbol.upper()}.log"


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError:
            continue
    deadline = time.time() + 5
    while time.time() < deadline:
        alive = [p for p in pids if _pid_alive(p)]
        if not alive:
            break
        time.sleep(0.25)
    for pid in [p for p in pids if _pid_alive(p)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pgrep_supervisors() -> list[str]:
    """Fallback when systemctl list is empty but python supervisors are running."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "orderbook_dca_grid.py"],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return []
    pids = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().isdigit()]
    if not pids:
        return []
    try:
        ps = subprocess.run(
            ["ps", "-p", ",".join(pids), "-ww", "-o", "args="],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return []
    found: list[str] = []
    for line in ps.stdout.splitlines():
        if "--supervise" not in line or "orderbook_dca_grid.py" not in line:
            continue
        parts = line.split()
        for i, part in enumerate(parts):
            if part.endswith("orderbook_dca_grid.py") and i + 1 < len(parts):
                sym = parts[i + 1].upper()
                if sym.isalnum() and sym.endswith("USDT"):
                    found.append(sym)
                break
    return sorted(set(found))


def _use_sudo_systemctl() -> bool:
    return _env("BOTCTL_NO_SUDO", "").lower() not in ("1", "true", "yes") and os.geteuid() != 0


def _pid_path(symbol: str) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR / f"{symbol.upper()}.pid"


def _meta_path(symbol: str) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_DIR / f"{symbol.upper()}.meta.json"


def _log_path(symbol: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{symbol.upper()}.log"


def write_run_meta(
    symbol: str,
    *,
    direction: str | None = None,
    gate_price: float | None = None,
    gate_enabled: bool | None = None,
    clear_gate: bool = False,
) -> dict[str, Any]:
    """Persist start args so the app can restore Gate / direction after reopen."""
    sym = symbol.upper()
    path = _meta_path(sym)
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload.update(raw)
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload["symbol"] = sym
    payload["updated_at"] = int(time.time())
    dir_arg = (direction or "").lower()
    if dir_arg in ("long", "short", "auto"):
        payload["direction"] = dir_arg
    if clear_gate:
        payload.pop("gate_price", None)
        payload["gate_enabled"] = False
    elif gate_price is not None:
        try:
            g = float(gate_price)
        except (TypeError, ValueError):
            g = None
        if g is not None and g > 0:
            payload["gate_price"] = g
            payload["gate_enabled"] = True if gate_enabled is None else bool(gate_enabled)
    if gate_enabled is not None and not clear_gate:
        payload["gate_enabled"] = bool(gate_enabled)
        if not payload["gate_enabled"]:
            payload.pop("gate_price", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def clear_run_meta(symbol: str) -> None:
    _meta_path(symbol).unlink(missing_ok=True)


def _parse_gate_direction_from_cmdline(cmdline: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = re.search(r"--gate-price[= ]+(\S+)", cmdline)
    if m:
        try:
            g = float(m.group(1))
            if g > 0:
                out["gate_price"] = g
        except ValueError:
            pass
    m = re.search(r"--direction[= ]+(\w+)", cmdline)
    if m:
        d = m.group(1).lower()
        if d in ("long", "short", "auto"):
            out["direction"] = d
    return out


def _cmdline_for_symbol(symbol: str) -> str:
    """Best-effort cmdline of a running orderbook_dca_grid.py for symbol.

    macOS `pgrep -af` often returns only PIDs, so resolve args via `ps`.
    """
    pids = _pgrep_pids(symbol)
    if not pids:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", ",".join(str(p) for p in pids), "-ww", "-o", "args="],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return ""
    for line in proc.stdout.splitlines():
        if "orderbook_dca_grid.py" in line:
            return line.strip()
    return ""


def run_meta(symbol: str) -> dict[str, Any]:
    """Return saved/inferred supervisor config: direction, gate_price, gate_enabled."""
    sym = symbol.upper()
    meta: dict[str, Any] = {"symbol": sym}
    path = _meta_path(sym)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    # Fill gaps from live process cmdline (covers bots started before meta existed).
    # Do not resurrect a gate the user explicitly cleared.
    if meta.get("gate_enabled") is not False:
        parsed = _parse_gate_direction_from_cmdline(_cmdline_for_symbol(sym))
        for k, v in parsed.items():
            meta.setdefault(k, v)
        if "gate_price" in meta and "gate_enabled" not in meta:
            meta["gate_enabled"] = True
    return meta


def _gates_match(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= max(1e-12, abs(float(a)) * 1e-9)


def apply_gate_config(
    symbol: str,
    *,
    direction: str | None = None,
    gate_price: float | None = None,
    gate_enabled: bool = False,
    restart_if_running: bool = True,
) -> str:
    """Persist gate/direction; restart live supervisor only when args actually change."""
    sym = symbol.upper()
    dir_arg = (direction or "").lower()
    if dir_arg and dir_arg not in ("long", "short", "auto"):
        dir_arg = ""

    if gate_enabled:
        if gate_price is None:
            return "❌ Activate gate requires a positive gate_price."
        try:
            g = float(gate_price)
        except (TypeError, ValueError):
            return "❌ Invalid gate_price."
        if g <= 0:
            return "❌ gate_price must be > 0."
        write_run_meta(sym, direction=dir_arg or None, gate_price=g, gate_enabled=True)
        gate_for_start: float | None = g
    else:
        write_run_meta(sym, direction=dir_arg or None, clear_gate=True)
        gate_for_start = None

    saved = (
        f"✅ Gate saved @ {gate_for_start:g}{f' ({dir_arg})' if dir_arg else ''}."
        if gate_enabled
        else "✅ Gate cleared."
    )

    if not restart_if_running or not is_running(sym):
        # Activate alone never auto-starts a stopped bot — use Start for that.
        return saved

    live = _parse_gate_direction_from_cmdline(_cmdline_for_symbol(sym))
    live_gate = live.get("gate_price")
    live_dir = (live.get("direction") or "").lower()
    want_dir = dir_arg or live_dir or "auto"
    same_gate = _gates_match(live_gate, gate_for_start)
    same_dir = (not dir_arg) or (live_dir == dir_arg) or (live_dir == want_dir and not dir_arg)
    if same_gate and same_dir:
        return f"{saved} Supervisor already running with this config."

    # Restart supervisor with the new gate (stop keeps meta).
    stop_msg = stop(sym, clear_meta=False)
    for _ in range(10):
        if not is_running(sym):
            break
        time.sleep(0.2)
    start_msg = start(sym, direction=dir_arg or None, gate_price=gate_for_start)
    time.sleep(0.4)
    if not is_running(sym):
        return (
            f"{stop_msg}\n{start_msg}\n"
            f"❌ {sym} did not stay running after applying gate — check logs."
        )
    return f"{saved}\n{stop_msg}\n{start_msg}"


def is_running(symbol: str, backend: str | None = None) -> bool:
    sym = symbol.upper()
    backend = backend or detect_backend()
    if backend == "systemd":
        for tpl in _unit_templates():
            unit = f"{tpl}@{sym}.service"
            proc = _systemctl_read("is-active", unit)
            if proc.stdout.strip() == "active":
                return True
        return bool(_pgrep_pids(sym))
    if _pgrep_pids(sym):
        return True
    pid_file = _pid_path(sym)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (TypeError, ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        pid_file.unlink(missing_ok=True)
        return False


def fib_is_running(symbol: str) -> bool:
    sym = symbol.upper()
    if _pgrep_fib_pids(sym):
        return True
    pid_file = _fib_pid_path(sym)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (TypeError, ValueError, OSError):
        return False
    if _pid_alive(pid):
        return True
    pid_file.unlink(missing_ok=True)
    return False


def list_running(backend: str | None = None) -> list[str]:
    backend = backend or detect_backend()
    out: list[str] = []
    if backend == "systemd":
        running = set(_list_systemd_running()) | set(_pgrep_supervisors())
        out.extend(sorted(running))
    else:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        for path in RUN_DIR.glob("*.pid"):
            stem = path.stem.upper()
            if stem.startswith("FIB-"):
                continue
            if is_running(stem, "pidfile"):
                seen.add(stem)
        # Also include supervisors started without a pidfile (e.g. via `dca` CLI).
        for sym in _pgrep_supervisors():
            seen.add(sym.upper())
        out.extend(sorted(seen))
    # Fib bots (any backend)
    seen_fib: set[str] = set()
    for path in RUN_DIR.glob("fib-*.pid"):
        sym = path.stem[4:].upper()
        if fib_is_running(sym):
            seen_fib.add(sym)
    try:
        proc = subprocess.run(["pgrep", "-af", "orderbook_micro_grid.py"], text=True, capture_output=True)
        for line in proc.stdout.splitlines():
            parts = line.split()
            for i, part in enumerate(parts):
                if part.endswith("orderbook_micro_grid.py") and i + 1 < len(parts):
                    sym = parts[i + 1].upper()
                    if sym.isalnum() and sym.endswith("USDT"):
                        seen_fib.add(sym)
                    break
    except FileNotFoundError:
        pass
    for sym in sorted(seen_fib):
        tag = f"FIB:{sym}"
        if tag not in out:
            out.append(tag)
    return sorted(out)


def start(
    symbol: str,
    backend: str | None = None,
    direction: str | None = None,
    gate_price: float | None = None,
) -> str:
    sym = symbol.upper()
    backend = backend or detect_backend()
    dir_arg = (direction or "").lower()
    if dir_arg and dir_arg not in ("long", "short", "auto"):
        dir_arg = ""
    gate: float | None = None
    if gate_price is not None:
        try:
            gate = float(gate_price)
        except (TypeError, ValueError):
            gate = None
        if gate is not None and gate <= 0:
            gate = None
    allow = allowed_symbols()
    if allow is not None and sym not in allow:
        return f"⛔ {sym} is not in FUTURES_PAIRS."

    if is_running(sym, backend):
        return f"ℹ️ {sym} is already running ({backend})."

    if backend == "systemd":
        unit = f"{futures_unit_template()}@{sym}.service"
        for action in ("enable", "start"):
            proc = _systemctl_write(action, unit)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "systemctl failed").strip()
                return f"❌ Could not start {sym}: {err}"
        if gate is not None:
            write_run_meta(sym, direction=dir_arg or None, gate_price=gate, gate_enabled=True)
        else:
            write_run_meta(sym, direction=dir_arg or None, clear_gate=True)
        note = ""
        if gate is not None:
            note = (
                f"\n⚠️ systemd start ignores --gate-price; set GATE_PRICE in the unit env "
                f"or use pidfile backend."
            )
        return f"▶️ {sym} supervisor started (systemd). Position and orders unchanged.{note}"

    if not GRID_SCRIPT.is_file():
        return f"❌ Cannot find {GRID_SCRIPT.name}."

    log = _log_path(sym)
    pid_file = _pid_path(sym)
    cmd = [
        sys.executable, "-u", str(GRID_SCRIPT), sym,
        "--supervise", "--recv-window", os.getenv("RECV_WINDOW", "15000"),
    ]
    if dir_arg:
        cmd.extend(["--direction", dir_arg])
    if gate is not None:
        cmd.extend(["--gate-price", str(gate)])

    with open(log, "a", encoding="utf-8") as logfh:
        logfh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logfh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.4)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        clear_run_meta(sym)
        return f"❌ {sym} exited on start — check {log}"
    if gate is not None:
        write_run_meta(sym, direction=dir_arg or None, gate_price=gate, gate_enabled=True)
    else:
        write_run_meta(sym, direction=dir_arg or None, clear_gate=True)
    gate_txt = f" gate={gate:g}" if gate is not None else ""
    dir_txt = f" {dir_arg}" if dir_arg else ""
    return (
        f"▶️ {sym} supervisor started (pid {proc.pid}){dir_txt}{gate_txt}. "
        f"Position and orders unchanged."
    )

def stop(symbol: str, backend: str | None = None, *, clear_meta: bool = False) -> str:
    sym = symbol.upper()
    backend = backend or detect_backend()
    stopped: list[str] = []

    if backend == "systemd":
        for tpl in _unit_templates():
            unit = f"{tpl}@{sym}.service"
            if _systemctl_read("is-active", unit).stdout.strip() != "active":
                continue
            proc = _systemctl_write("stop", unit)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "systemctl failed").strip()
                return f"❌ Could not stop {sym}: {err}"
            stopped.append(f"systemd {unit}")

    pids = _pgrep_pids(sym)
    if pids:
        _kill_pids(pids)
        stopped.append(f"DCA process(es) {', '.join(str(p) for p in pids)}")

    pid_file = _pid_path(sym)
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
    if clear_meta:
        clear_run_meta(sym)

    fib_pids = _pgrep_fib_pids(sym)
    if fib_pids:
        _kill_pids(fib_pids)
        stopped.append(f"FIB process(es) {', '.join(str(p) for p in fib_pids)}")
    fib_pid_file = _fib_pid_path(sym)
    if fib_pid_file.exists():
        try:
            pid = int(fib_pid_file.read_text().strip())
            if _pid_alive(pid) and pid not in fib_pids:
                _kill_pids([pid])
                stopped.append(f"FIB pid {pid}")
        except (TypeError, ValueError, OSError):
            pass
        fib_pid_file.unlink(missing_ok=True)

    if stopped:
        how = "; ".join(stopped)
        return f"⏸ {sym} stopped ({how}). Position and orders unchanged on Binance."

    if backend == "pidfile":
        return f"ℹ️ {sym} was not running (DCA or FIB)."

    # Supervisor not on this host — often Mac local bot + Telegram ctl on VPS.
    body = trading_status(sym)
    has_position = "Position:" in body and "flat" not in body
    if has_position or "DCA limits:" in body and "DCA limits: 0" not in body:
        return (
            f"ℹ️ {sym} supervisor not found on this server.\n"
            f"If the bot runs on your Mac (or another machine), stop it there:\n"
            f"  python3 botctl.py stop {sym}\n\n"
            f"{body}"
        )
    return f"ℹ️ {sym} was not running."


def fib_start(symbol: str, direction: str | None = None) -> str:
    """Start fib micro-grid in background (pidfile)."""
    sym = symbol.upper()
    dir_arg = (direction or "").lower()
    if dir_arg and dir_arg not in ("long", "short", "auto"):
        dir_arg = ""
    allow = allowed_symbols()
    if allow is not None and sym not in allow:
        return f"⛔ {sym} is not in FUTURES_PAIRS."

    if fib_is_running(sym):
        return f"ℹ️ {sym} FIB is already running."

    if not FIB_SCRIPT.is_file():
        return f"❌ Cannot find {FIB_SCRIPT.name}."

    log = _fib_log_path(sym)
    pid_file = _fib_pid_path(sym)
    cmd = [
        sys.executable, "-u", str(FIB_SCRIPT), sym,
        "--recv-window", os.getenv("RECV_WINDOW", "15000"),
    ]
    if dir_arg:
        cmd.extend(["--direction", dir_arg])

    with open(log, "a", encoding="utf-8") as logfh:
        logfh.write(f"\n--- fib start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logfh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.6)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        return f"❌ {sym} FIB exited on start — check {log}"
    dir_note = f" · {dir_arg}" if dir_arg else ""
    return (
        f"▶️ {sym} FIB started (pid {proc.pid}){dir_note}. "
        f"Position/orders unchanged. Log: {log}"
    )


def fib_stop(symbol: str) -> str:
    """Stop only the fib micro-grid for symbol (DCA left alone)."""
    sym = symbol.upper()
    stopped: list[str] = []
    fib_pids = _pgrep_fib_pids(sym)
    if fib_pids:
        _kill_pids(fib_pids)
        stopped.append(f"process(es) {', '.join(str(p) for p in fib_pids)}")
    fib_pid_file = _fib_pid_path(sym)
    if fib_pid_file.exists():
        try:
            pid = int(fib_pid_file.read_text().strip())
            if _pid_alive(pid) and pid not in fib_pids:
                _kill_pids([pid])
                stopped.append(f"pid {pid}")
        except (TypeError, ValueError, OSError):
            pass
        fib_pid_file.unlink(missing_ok=True)
    if stopped:
        return f"⏸ {sym} FIB stopped ({'; '.join(stopped)}). Position and orders unchanged."
    return f"ℹ️ {sym} FIB was not running."


def status(symbol: str, backend: str | None = None) -> str:
    sym = symbol.upper()
    backend = backend or detect_backend()
    dca_on = is_running(sym, backend)
    fib_on = fib_is_running(sym)
    bits = []
    if dca_on:
        bits.append("DCA running")
    if fib_on:
        bits.append("FIB running")
    if not bits:
        bits.append("stopped")
    head = f"{'▶️' if (dca_on or fib_on) else '⏸'} {sym} · {', '.join(bits)} ({backend})"
    body = trading_status(sym)
    return f"{head}\n{body}"


def list_status(backend: str | None = None) -> str:
    backend = backend or detect_backend()
    running = list_running(backend)
    if not running and backend == "systemd":
        pgrep = _pgrep_supervisors()
        if pgrep:
            blocks = [f"▶️ {sym} · running (process, not visible via systemctl)\n{trading_status(sym)}"
                      for sym in pgrep]
            head = (
                "⚠️ systemd list empty but supervisor processes found.\n"
                "On the VPS run: python3 deploy/sync_pairs.py status\n"
                "Or fix sudo for forge (see README).\n"
            )
            return head + "\n\n".join(blocks)
    if not running:
        return f"No supervisors running ({backend})."
    blocks = []
    for item in running:
        if item.startswith("FIB:"):
            sym = item.split(":", 1)[1]
            blocks.append(status(sym, backend))
        else:
            blocks.append(status(item, backend))
    # Dedupe by symbol
    seen: set[str] = set()
    uniq: list[str] = []
    for b in blocks:
        key = b.split("\n", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    return "\n\n".join(uniq)


def cleanup(symbol: str) -> str:
    """Cancel open obstage* staged-exit algos on Binance (position unchanged)."""
    sym = symbol.upper()
    try:
        from orderbook_dca_grid import load_keys
        from orderbook_staged_exit import (
            _algo_client_id,
            cancel_all_staged_algos,
            list_open_algo_orders,
        )
    except ImportError as exc:
        return f"❌ Could not load bot modules: {exc}"

    api, sec = load_keys(None)
    if not api or not sec:
        return "❌ No API keys in .env."

    recv = int(_env("RECV_WINDOW", "15000") or "15000")
    try:
        before = [
            o for o in list_open_algo_orders(sym, api, sec, recv)
            if _algo_client_id(o).startswith("obstage")
        ]
        killed = cancel_all_staged_algos(sym, api, sec, recv)
    except Exception as exc:
        return f"❌ Cleanup failed for {sym}: {exc}"

    after = [
        o for o in list_open_algo_orders(sym, api, sec, recv)
        if _algo_client_id(o).startswith("obstage")
    ]
    if killed:
        msg = f"🧹 {sym} cleanup: cancelled {killed} obstage* algo(s)."
    elif before:
        msg = f"ℹ️ {sym} cleanup: algos already gone ({len(before)} were stale)."
    else:
        msg = f"ℹ️ {sym} cleanup: no open obstage* algos found."

    if after:
        msg += f"\n⚠️ {len(after)} obstage* algo(s) still open — retry or cancel in Binance UI."
    elif before or killed:
        msg += "\nRefresh Binance if Stop/TP lines still show (UI ghosts)."

    body = trading_status(sym)
    return f"{msg}\n\n{body}"


def _bot_order_cid(order: dict) -> str:
    return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")


def _bot_algo_cid(order: dict) -> str:
    return str(
        order.get("clientAlgoId")
        or order.get("clientOrderId")
        or order.get("newClientOrderId")
        or ""
    )


def _is_our_limit(cid: str) -> bool:
    """DCA / FIB limit client ids (not scalp)."""
    return cid.startswith("obdca") or cid.startswith("obmg")


def _is_our_algo(cid: str) -> bool:
    return cid.startswith("obstage") or cid.startswith("obmg")


def _symbol_is_flat(symbol: str, api: str, sec: str, recv: int) -> bool:
    from orderbook_dca_grid import _detect_open_side, _resolve_hedge

    class _A:
        pass

    args = _A()
    args.env_file = None
    args.recv_window = recv
    args.position_mode = "auto"
    hedge = _resolve_hedge(args, api, sec)
    side, _qty, _entry = _detect_open_side(symbol, hedge, api, sec, recv)
    return side is None


def _sweep_symbol(symbol: str, api: str, sec: str, recv: int) -> str:
    """Cancel bot limits/algos on SYMBOL only if the position is flat."""
    from orderbook_dca_grid import _signed_request, cancel_dca_grid_orders
    from orderbook_micro_grid import cancel_our_exits, cancel_our_grid
    from orderbook_staged_exit import cancel_all_staged_algos

    sym = symbol.upper()
    if not _symbol_is_flat(sym, api, sec, recv):
        return f"⏭ {sym}: position open — left orders alone."

    oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, recv) or []
    our_limits = [o for o in oo if _is_our_limit(_bot_order_cid(o))]
    try:
        algos = _signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": sym}, api, sec, recv,
        ) or []
    except Exception:
        algos = []
    our_algos = [o for o in algos if _is_our_algo(_bot_algo_cid(o))]

    if not our_limits and not our_algos:
        return f"ℹ️ {sym}: flat, no orphan bot orders."

    n_dca = cancel_dca_grid_orders(sym, api, sec, recv)
    n_fib = cancel_our_grid(sym, api, sec, recv)
    n_fib_algo = cancel_our_exits(sym, api, sec, recv)
    n_stage = cancel_all_staged_algos(sym, api, sec, recv)
    killed = n_dca + n_fib + n_fib_algo + n_stage

    bits = []
    if n_dca:
        bits.append(f"{n_dca} DCA limit(s)")
    if n_fib:
        bits.append(f"{n_fib} FIB limit(s)")
    if n_fib_algo:
        bits.append(f"{n_fib_algo} FIB algo(s)")
    if n_stage:
        bits.append(f"{n_stage} staged algo(s)")
    detail = ", ".join(bits) if bits else f"{killed} order(s)"

    warn = ""
    if is_running(sym) or fib_is_running(sym):
        warn = (
            f"\n⚠️ Supervisor still running — may re-arm. "
            f"Use: python3 botctl.py stop {sym}"
        )
    return f"🧹 {sym}: flat → cancelled {detail}.{warn}"


def sweep(symbol: str | None = None) -> str:
    """Cancel bot limit/algo orders on flat symbols (orphans after manual close).

    With SYMBOL: only that pair.
    Without: scan all open orders/algos account-wide.
    Never cancels when a position is still open.
    """
    try:
        from orderbook_dca_grid import _signed_request, load_keys
    except ImportError as exc:
        return f"❌ Could not load bot modules: {exc}"

    api, sec = load_keys(None)
    if not api or not sec:
        return "❌ No API keys in .env."

    recv = int(_env("RECV_WINDOW", "15000") or "15000")

    if symbol:
        try:
            return _sweep_symbol(symbol.upper(), api, sec, recv)
        except Exception as exc:
            return f"❌ Sweep failed for {symbol.upper()}: {exc}"

    # Account-wide: discover symbols that still have our tags.
    try:
        oo = _signed_request("GET", "/fapi/v1/openOrders", {}, api, sec, recv) or []
    except Exception as exc:
        return f"❌ Could not list open orders: {exc}"
    try:
        algos = _signed_request("GET", "/fapi/v1/openAlgoOrders", {}, api, sec, recv) or []
    except Exception:
        algos = []

    symbols: set[str] = set()
    for o in oo:
        cid = _bot_order_cid(o)
        if _is_our_limit(cid):
            symbols.add(str(o.get("symbol", "")).upper())
    for o in algos:
        cid = _bot_algo_cid(o)
        if _is_our_algo(cid):
            symbols.add(str(o.get("symbol", "")).upper())
    symbols.discard("")

    if not symbols:
        return "ℹ️ Sweep: no bot open orders/algos found."

    lines: list[str] = [f"Sweep scan · {len(symbols)} symbol(s) with bot orders:"]
    for sym in sorted(symbols):
        try:
            lines.append(_sweep_symbol(sym, api, sec, recv))
        except Exception as exc:
            lines.append(f"❌ {sym}: {exc}")
    return "\n".join(lines)


def trading_status(symbol: str) -> str:
    """Short trading summary for Telegram (no ANSI)."""
    sym = symbol.upper()
    lines: list[str] = []

    try:
        from orderbook_dca_grid import (
            _detect_open_side,
            _resolve_hedge,
            count_dca_orders,
            get_position_meta,
            get_symbol_leverage,
            load_keys,
            _signed_request,
        )
        from exits import resolve_exit_mode, exit_mode_label
    except ImportError as exc:
        return f"⚠️ Could not load bot modules: {exc}"

    api, sec = load_keys(None)
    if not api or not sec:
        return "⚠️ No API keys in .env."

    recv = int(_env("RECV_WINDOW", "15000") or "15000")
    class _A:
        pass

    args = _A()
    args.env_file = None
    args.recv_window = recv
    args.position_mode = "auto"
    args.exit_mode = None
    args.no_tp = False
    args.direction = "auto"

    try:
        hedge = _resolve_hedge(args, api, sec)
        side_is_long, qty, entry = _detect_open_side(sym, hedge, api, sec, recv)
        oo = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": sym}, api, sec, recv) or []
        n_dca = count_dca_orders(oo, sym)

        lines.append(f"Exit: {exit_mode_label(resolve_exit_mode(args))}")
        lines.append(f"DCA limits: {n_dca}")

        if side_is_long is not None:
            direction = "LONG" if side_is_long else "SHORT"
            meta = get_position_meta(sym, side_is_long, hedge, api, sec, recv)
            lev = int(meta.get("leverage", 0) or 0) or get_symbol_leverage(sym, api, sec, recv)
            pnl = float(meta.get("unrealized_pnl", 0) or 0)
            notional = float(meta.get("notional", 0) or 0)
            lines.append(f"Position: {direction} {qty:g} @ {entry:g}")
            if notional > 0:
                lines.append(f"Vol: {notional:,.2f} USDT · {lev}x")
            try:
                import telegram_notify as telegram
                lines.append(telegram.fmt_pnl(pnl, notional, lev))
            except Exception:
                lines.append(f"PnL: {pnl:+,.2f} USDT")
        else:
            lines.append("Position: flat")

        try:
            import orderbook_staged_exit as staged
            state = staged.load_state(sym)
            phase = state.get("phase", staged.PHASE_IDLE)
            if phase not in (staged.PHASE_IDLE, "idle"):
                lines.append(f"Staged: {phase}")
                if state.get("tp1_price"):
                    lines.append(f"TP1 @ {float(state['tp1_price']):g}")
                if state.get("be_price"):
                    be_pct = float(state.get("be_profit_pct", 0.1) or 0)
                    be_tag = f"entry+{be_pct:g}%" if be_pct > 0 else "entry"
                    lines.append(f"SL @ {be_tag} {float(state['be_price']):g}")
        except Exception:
            pass
    except Exception as exc:
        lines.append(f"⚠️ Audit error: {exc}")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Control DCA/FIB bots per symbol (no position close on stop)")
    p.add_argument(
        "command",
        choices=["start", "stop", "status", "cleanup", "sweep", "list", "running", "fib", "fib-stop"],
    )
    p.add_argument("symbol", nargs="?", help="Symbol e.g. SXTUSDT (optional for sweep)")
    p.add_argument("direction", nargs="?", help="For start/fib: long|short|auto")
    p.add_argument(
        "--gate-price",
        type=float,
        default=None,
        help="For start: only arm SHORT if mid>price, LONG if mid<price",
    )
    p.add_argument("--backend", choices=["auto", "systemd", "pidfile"], default="auto")
    return p.parse_args()


def main() -> None:
    from orderbook_dca_grid import load_env_file

    load_env_file(None)
    args = parse_args()
    backend = detect_backend() if args.backend == "auto" else args.backend

    if args.command in ("start", "stop", "status", "cleanup", "fib", "fib-stop") and not args.symbol:
        print("Symbol required.", file=sys.stderr)
        sys.exit(1)

    if args.command == "start":
        print(start(
            args.symbol, backend,
            direction=args.direction,
            gate_price=args.gate_price,
        ))
    elif args.command == "stop":
        print(stop(args.symbol, backend))
    elif args.command == "fib":
        print(fib_start(args.symbol, args.direction))
    elif args.command == "fib-stop":
        print(fib_stop(args.symbol))
    elif args.command == "status":
        print(status(args.symbol, backend))
    elif args.command == "cleanup":
        print(cleanup(args.symbol))
    elif args.command == "sweep":
        print(sweep(args.symbol))
    elif args.command in ("list", "running"):
        print(list_status(backend))


if __name__ == "__main__":
    main()
