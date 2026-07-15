#!/usr/bin/env python3
"""Start/stop/status for per-symbol DCA supervisors (no position close on stop)."""

from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run" / "pids"
LOG_DIR = ROOT / ".run" / "logs"
GRID_SCRIPT = ROOT / "orderbook_dca_grid.py"


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
        proc = subprocess.run(["pgrep", "-af", "orderbook_dca_grid.py"], text=True, capture_output=True)
    except FileNotFoundError:
        return []
    found: list[str] = []
    for line in proc.stdout.splitlines():
        if "--supervise" not in line:
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


def _log_path(symbol: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{symbol.upper()}.log"


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


def list_running(backend: str | None = None) -> list[str]:
    backend = backend or detect_backend()
    if backend == "systemd":
        running = set(_list_systemd_running()) | set(_pgrep_supervisors())
        return sorted(running)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for path in RUN_DIR.glob("*.pid"):
        sym = path.stem.upper()
        if is_running(sym, "pidfile"):
            out.append(sym)
    return sorted(out)


def start(symbol: str, backend: str | None = None, direction: str | None = None) -> str:
    sym = symbol.upper()
    backend = backend or detect_backend()
    dir_arg = (direction or "").lower()
    if dir_arg and dir_arg not in ("long", "short", "auto"):
        dir_arg = ""
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
        return f"▶️ {sym} supervisor started (systemd). Position and orders unchanged."

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
        return f"❌ {sym} exited on start — check {log}"
    return f"▶️ {sym} supervisor started (pid {proc.pid}). Position and orders unchanged."


def stop(symbol: str, backend: str | None = None) -> str:
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
        stopped.append(f"process(es) {', '.join(str(p) for p in pids)}")

    pid_file = _pid_path(sym)
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)

    if stopped:
        how = "; ".join(stopped)
        return f"⏸ {sym} supervisor stopped ({how}). Position and orders unchanged on Binance."

    if backend == "pidfile":
        return f"ℹ️ {sym} was not running."

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


def status(symbol: str, backend: str | None = None) -> str:
    sym = symbol.upper()
    backend = backend or detect_backend()
    running = is_running(sym, backend)
    head = f"{'▶️' if running else '⏸'} {sym} · {'running' if running else 'stopped'} ({backend})"
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
    blocks = [status(sym, backend) for sym in running]
    return "\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Control DCA supervisors per symbol (no position close on stop)")
    p.add_argument("command", choices=["start", "stop", "status", "list", "running"])
    p.add_argument("symbol", nargs="?", help="Symbol e.g. SXTUSDT")
    p.add_argument("--backend", choices=["auto", "systemd", "pidfile"], default="auto")
    return p.parse_args()


def main() -> None:
    from orderbook_dca_grid import load_env_file

    load_env_file(None)
    args = parse_args()
    backend = detect_backend() if args.backend == "auto" else args.backend

    if args.command in ("start", "stop", "status") and not args.symbol:
        print("Symbol required.", file=sys.stderr)
        sys.exit(1)

    if args.command == "start":
        print(start(args.symbol, backend))
    elif args.command == "stop":
        print(stop(args.symbol, backend))
    elif args.command == "status":
        print(status(args.symbol, backend))
    elif args.command in ("list", "running"):
        print(list_status(backend))


if __name__ == "__main__":
    main()
