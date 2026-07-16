"""Persist OB scalp recovery state and trade journal under .run/logs/SYMBOL/."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


@dataclass
class RecoveryState:
    level: int = 0
    loss_streak: int = 0
    cumulative_loss_usdt: float = 0.0
    base_notional_usdt: float = 0.0
    locked_side: str = ""  # "long" | "short" — same side until TP reset
    wins: int = 0
    losses: int = 0
    updated_at: str = ""

    @property
    def multiplier(self) -> float:
        return float(2 ** max(0, self.level))

    def touch(self) -> None:
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")


def symbol_dir(symbol: str) -> Path:
    path = LOG_ROOT / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(symbol: str) -> Path:
    return symbol_dir(symbol) / "scalp_recovery.json"


def journal_path(symbol: str) -> Path:
    return symbol_dir(symbol) / "scalp_trades.log"


def load_state(symbol: str) -> RecoveryState:
    path = state_path(symbol)
    if not path.exists():
        return RecoveryState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return RecoveryState()
    if not isinstance(raw, dict):
        return RecoveryState()
    return RecoveryState(
        level=int(raw.get("level", 0) or 0),
        loss_streak=int(raw.get("loss_streak", 0) or 0),
        cumulative_loss_usdt=float(raw.get("cumulative_loss_usdt", 0) or 0),
        base_notional_usdt=float(raw.get("base_notional_usdt", 0) or 0),
        locked_side=str(raw.get("locked_side", "") or "").lower(),
        wins=int(raw.get("wins", 0) or 0),
        losses=int(raw.get("losses", 0) or 0),
        updated_at=str(raw.get("updated_at", "") or ""),
    )


def save_state(symbol: str, state: RecoveryState) -> None:
    state.touch()
    state_path(symbol).write_text(
        json.dumps(asdict(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def reset_state(symbol: str) -> RecoveryState:
    state = RecoveryState()
    save_state(symbol, state)
    append_journal(symbol, "RESET recovery state → level 0")
    return state


def append_journal(symbol: str, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    with open(journal_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(line)


def pnl_usdt(entry: float, exit_price: float, qty: float, is_long: bool) -> float:
    if entry <= 0 or exit_price <= 0 or qty <= 0:
        return 0.0
    if is_long:
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def ensure_base_notional(state: RecoveryState, base_notional: float) -> None:
    if state.base_notional_usdt <= 0 and base_notional > 0:
        state.base_notional_usdt = base_notional


def target_notional(state: RecoveryState, base_notional: float, *, max_level: int) -> tuple[float, int]:
    ensure_base_notional(state, base_notional)
    base = state.base_notional_usdt if state.base_notional_usdt > 0 else base_notional
    level = min(max(0, state.level), max(0, max_level))
    return base * (2 ** level), level


def record_close(
    symbol: str,
    state: RecoveryState,
    *,
    reason: str,
    direction: str,
    entry: float,
    exit_price: float,
    qty: float,
    gross_pct: float,
    net_usdt: float,
    dry_run: bool,
) -> RecoveryState:
    won = net_usdt > 0
    line = (
        f"{reason} {direction} qty={qty:g} entry={entry:g} exit={exit_price:g} "
        f"gross={gross_pct:+.3f}% pnl={net_usdt:+.4f} USDT "
        f"level={state.level} streak={state.loss_streak} cumulative={state.cumulative_loss_usdt:+.4f}"
    )

    if won:
        state.wins += 1
        state.level = 0
        state.loss_streak = 0
        state.cumulative_loss_usdt = 0.0
        state.locked_side = ""
        line += " → RESET"
    else:
        state.losses += 1
        state.loss_streak += 1
        state.level += 1
        state.cumulative_loss_usdt += abs(net_usdt)
        state.locked_side = direction.lower()
        line += (
            f" → level {state.level} ({state.multiplier:g}x) "
            f"locked {state.locked_side.upper()} cumulative={state.cumulative_loss_usdt:.4f}"
        )

    prefix = "[dry-run] " if dry_run else ""
    append_journal(symbol, f"{prefix}{line}")
    if not dry_run:
        save_state(symbol, state)
    return state


def format_status(state: RecoveryState) -> str:
    if state.level <= 0 and state.cumulative_loss_usdt <= 0:
        return "recovery off (level 0)"
    lock = f" · locked {state.locked_side.upper()}" if state.locked_side else ""
    return (
        f"recovery level {state.level} ({state.multiplier:g}x){lock} · "
        f"loss streak {state.loss_streak} · cumulative {state.cumulative_loss_usdt:.4f} USDT"
    )
