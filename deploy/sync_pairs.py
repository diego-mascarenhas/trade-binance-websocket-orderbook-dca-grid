#!/usr/bin/env python3
"""Enable/disable systemd DCA units from FUTURES_PAIRS / SPOT_PAIRS in .env.

Reads comma-separated symbols and keeps systemd in sync:
  - pairs listed  → enable + start (restart if already running)
  - pairs running but not listed → stop + disable

Requires unit templates already installed, e.g.:
  sudo cp deploy/dca-futures@.service deploy/dca-spot@.service /etc/systemd/system/
  sudo systemctl daemon-reload

Usage:
  python3 deploy/sync_pairs.py              # sync futures + spot from .env
  python3 deploy/sync_pairs.py --dry-run    # preview only
  python3 deploy/sync_pairs.py status       # show desired vs running
  python3 deploy/sync_pairs.py --futures-only
  python3 deploy/sync_pairs.py --spot-only
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

UNIT_RE = re.compile(r"^(.+)@(.+)\.service$")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def load_env_file(env_file: str | None) -> dict[str, str]:
    """Parse KEY=VALUE lines from .env (does not overwrite os.environ)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    candidates = [env_file, os.path.join(root, ".env"), os.path.join(os.getcwd(), ".env")]
    seen: set[str] = set()
    out: dict[str, str] = {}
    for path in candidates:
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    out[k] = v
        break
    return out


def parse_pairs(raw: str) -> list[str]:
    """Parse 'BTCUSDT, ETHUSDT' → ['BTCUSDT', 'ETHUSDT']."""
    pairs: list[str] = []
    for part in raw.replace(";", ",").split(","):
        sym = part.strip().upper()
        if sym:
            pairs.append(sym)
    return pairs


def systemd_cmd(use_sudo: bool, *args: str) -> list[str]:
    prefix = ["sudo"] if use_sudo else []
    return prefix + ["systemctl", *args]


def run_cmd(cmd: list[str], *, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    line = " ".join(cmd)
    if dry_run:
        print(f"{DIM}  would run: {line}{RESET}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def list_unit_symbols(template: str, use_sudo: bool) -> set[str]:
    """Return symbols with any systemd state for template@SYMBOL.service."""
    cmd = systemd_cmd(use_sudo, "list-units", "--all", f"{template}@*", "--no-legend", "--plain")
    try:
        proc = run_cmd(cmd, check=False)
    except FileNotFoundError:
        print(f"{RED}systemctl not found.{RESET}")
        sys.exit(1)
    if proc.returncode != 0:
        return set()
    symbols: set[str] = set()
    for line in proc.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        m = UNIT_RE.match(unit)
        if m and m.group(1) == template:
            symbols.add(m.group(2).upper())
    return symbols


def unit_state(template: str, symbol: str, use_sudo: bool) -> str:
    cmd = systemd_cmd(use_sudo, "is-active", f"{template}@{symbol}.service")
    proc = run_cmd(cmd, check=False)
    return (proc.stdout or proc.stderr or "unknown").strip()


def sync_market(
    label: str,
    template: str,
    desired: list[str],
    *,
    use_sudo: bool,
    dry_run: bool,
    restart: bool,
) -> None:
    desired_set = set(desired)
    running_set = list_unit_symbols(template, use_sudo)

    to_enable = sorted(desired_set)
    to_disable = sorted(running_set - desired_set)

    print(f"\n{BOLD}{CYAN}{label} ({template}@){RESET}  "
          f"{DIM}desired: {', '.join(to_enable) or '—'}{RESET}")

    for sym in to_enable:
        unit = f"{template}@{sym}.service"
        for action in ("enable", "start"):
            run_cmd(systemd_cmd(use_sudo, action, unit), dry_run=dry_run)
        state = unit_state(template, sym, use_sudo) if not dry_run else "dry-run"
        note = f"{GREEN}✓ {unit}{RESET}"
        if restart and sym in running_set and not dry_run:
            run_cmd(systemd_cmd(use_sudo, "restart", unit), dry_run=dry_run)
            note += f" {DIM}(restarted){RESET}"
        else:
            note += f" {DIM}[{state}]{RESET}"
        print(note)

    for sym in to_disable:
        unit = f"{template}@{sym}.service"
        for action in ("stop", "disable"):
            run_cmd(systemd_cmd(use_sudo, action, unit), dry_run=dry_run)
        print(f"{YELLOW}− disabled {unit}{RESET}")


def print_status(env: dict[str, str], futures_tpl: str, spot_tpl: str, use_sudo: bool) -> None:
    for label, key, tpl in (
        ("Futures", "FUTURES_PAIRS", futures_tpl),
        ("Spot", "SPOT_PAIRS", spot_tpl),
    ):
        raw = env.get(key)
        if raw is None:
            print(f"\n{BOLD}{label}{RESET}: {DIM}{key} not set in .env — skipped by sync{RESET}")
            continue
        desired = parse_pairs(raw)
        running = sorted(list_unit_symbols(tpl, use_sudo))
        print(f"\n{BOLD}{label}{RESET} ({tpl}@)")
        print(f"  .env:    {', '.join(desired) or '(empty — all will be disabled)'}")
        print(f"  systemd: {', '.join(running) or '(none)'}")
        extra = sorted(set(running) - set(desired))
        missing = sorted(set(desired) - set(running))
        if missing:
            print(f"  {YELLOW}not running:{RESET} {', '.join(missing)}")
        if extra:
            print(f"  {YELLOW}extra (would disable on sync):{RESET} {', '.join(extra)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync systemd DCA units from .env pair lists")
    p.add_argument("--env-file", default=None, help="Path to .env (default: project root)")
    p.add_argument("--dry-run", action="store_true", help="Print actions without running systemctl")
    p.add_argument("--futures-only", action="store_true")
    p.add_argument("--spot-only", action="store_true")
    p.add_argument("--no-sudo", action="store_true", help="Omit sudo (when already root)")
    p.add_argument("--restart", action="store_true", help="Restart units that were already running")
    p.add_argument("--futures-unit", default=os.getenv("FUTURES_UNIT", "dca-futures"),
                   help="systemd template for futures (default: dca-futures)")
    p.add_argument("--spot-unit", default=os.getenv("SPOT_UNIT", "dca-spot"),
                   help="systemd template for spot (default: dca-spot)")
    p.add_argument("command", nargs="?", choices=["sync", "status"], default="sync")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not shutil.which("systemctl"):
        print(f"{RED}systemctl not found — run this on the Linux server.{RESET}")
        sys.exit(1)

    env = load_env_file(args.env_file)
    if not env:
        print(f"{RED}No .env found. Copy .env.example → .env and set FUTURES_PAIRS / SPOT_PAIRS.{RESET}")
        sys.exit(1)

    use_sudo = not args.no_sudo and os.geteuid() != 0
    do_futures = not args.spot_only
    do_spot = not args.futures_only

    if args.command == "status":
        print_status(env, args.futures_unit, args.spot_unit, use_sudo)
        return

    if args.dry_run:
        print(f"{BOLD}{CYAN}DRY-RUN — no systemctl changes{RESET}")

    if do_futures:
        raw = env.get("FUTURES_PAIRS")
        if raw is None:
            print(f"\n{DIM}FUTURES_PAIRS not set — skipping futures.{RESET}")
        else:
            sync_market(
                "Futures", args.futures_unit, parse_pairs(raw),
                use_sudo=use_sudo, dry_run=args.dry_run, restart=args.restart,
            )

    if do_spot:
        raw = env.get("SPOT_PAIRS")
        if raw is None:
            print(f"\n{DIM}SPOT_PAIRS not set — skipping spot.{RESET}")
        else:
            sync_market(
                "Spot", args.spot_unit, parse_pairs(raw),
                use_sudo=use_sudo, dry_run=args.dry_run, restart=args.restart,
            )

    if not args.dry_run:
        print(f"\n{GREEN}Done.{RESET}")


if __name__ == "__main__":
    main()
