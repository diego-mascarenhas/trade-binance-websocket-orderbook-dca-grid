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
    locked_side: str = ""  # "long" | "short" — same side until TP or lock timeout
    locked_at: float = 0.0  # unix ts when side lock was set
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
        locked_at=float(raw.get("locked_at", 0) or 0),
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
    """Seed or refresh the martingale base from the current size config.

    When flat (no level / debt), always adopt ``base_notional`` so a switch from
    exchange-min to fixed size (or any config change) takes effect on the next
    entry. Mid-ladder, keep the locked base so recovery math stays consistent.
    """
    if base_notional <= 0:
        return
    if state.level <= 0 and state.cumulative_loss_usdt <= 0:
        state.base_notional_usdt = base_notional
        return
    if state.base_notional_usdt <= 0:
        state.base_notional_usdt = base_notional


def target_notional(state: RecoveryState, base_notional: float, *, max_level: int) -> tuple[float, int]:
    ensure_base_notional(state, base_notional)
    base = state.base_notional_usdt if state.base_notional_usdt > 0 else base_notional
    level = min(max(0, state.level), max(0, max_level))
    return base * (2 ** level), level


def _set_side_lock(state: RecoveryState, direction: str) -> None:
    state.locked_side = direction.lower()
    state.locked_at = time.time()


def _clear_side_lock(state: RecoveryState) -> None:
    state.locked_side = ""
    state.locked_at = 0.0


def side_lock_age_sec(state: RecoveryState) -> float:
    """Seconds since side lock was set (0 if unlocked)."""
    if not state.locked_side:
        return 0.0
    if state.locked_at > 0:
        return max(0.0, time.time() - state.locked_at)
    if state.updated_at:
        try:
            return max(
                0.0,
                time.time() - time.mktime(time.strptime(state.updated_at, "%Y-%m-%d %H:%M:%S")),
            )
        except (ValueError, OverflowError):
            return 0.0
    return 0.0


def maybe_expire_side_lock(
    symbol: str,
    state: RecoveryState,
    *,
    lock_min: float,
    dry_run: bool = False,
) -> bool:
    """Clear locked_side after lock_min minutes. Keeps level/debt. Returns True if unlocked."""
    if not state.locked_side or lock_min <= 0:
        return False
    age = side_lock_age_sec(state)
    if age < lock_min * 60.0:
        return False
    side = state.locked_side.upper()
    _clear_side_lock(state)
    msg = (
        f"UNLOCK {side} after {lock_min:g}m side-lock "
        f"(keep level {state.level} · debt {state.cumulative_loss_usdt:.4f})"
    )
    append_journal(symbol, msg)
    if not dry_run:
        save_state(symbol, state)
    return True


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
    trigger: str = "",
) -> RecoveryState:
    """Update recovery after a close.

    A tiny 'win' must not wipe the martingale debt. Full RESET only when
    net_usdt covers cumulative_loss_usdt (or there was no debt).
    """
    debt_before = max(0.0, state.cumulative_loss_usdt)
    trig = f" trigger={trigger}" if trigger else ""
    line = (
        f"{reason} {direction} qty={qty:g} entry={entry:g} exit={exit_price:g} "
        f"gross={gross_pct:+.3f}% pnl={net_usdt:+.4f} USDT "
        f"level={state.level} streak={state.loss_streak} cumulative={debt_before:+.4f}"
        f"{trig}"
    )

    if net_usdt > 0:
        state.wins += 1
        if debt_before <= 0 or net_usdt >= debt_before:
            # True recovery — hole filled (or no hole)
            state.level = 0
            state.loss_streak = 0
            state.cumulative_loss_usdt = 0.0
            state.base_notional_usdt = 0.0  # re-seed from size config on next open
            _clear_side_lock(state)
            line += " → RESET"
        else:
            # Partial — reduce debt, step level down one, keep side lock
            state.cumulative_loss_usdt = debt_before - net_usdt
            state.level = max(0, state.level - 1)
            state.loss_streak = max(0, state.loss_streak - 1)
            if state.level <= 0 and state.cumulative_loss_usdt > 0:
                state.level = 1  # still owe — stay at least 2x until cleared
            _set_side_lock(state, direction)
            line += (
                f" → PARTIAL recover -{net_usdt:.4f} "
                f"left={state.cumulative_loss_usdt:.4f} "
                f"level {state.level} ({state.multiplier:g}x) locked {state.locked_side.upper()}"
            )
    else:
        state.losses += 1
        state.loss_streak += 1
        state.level += 1
        state.cumulative_loss_usdt = debt_before + abs(net_usdt)
        _set_side_lock(state, direction)
        line += (
            f" → level {state.level} ({state.multiplier:g}x) "
            f"locked {state.locked_side.upper()} cumulative={state.cumulative_loss_usdt:.4f}"
        )

    prefix = "[dry-run] " if dry_run else ""
    append_journal(symbol, f"{prefix}{line}")
    if not dry_run:
        save_state(symbol, state)
    return state


def recovery_covers_debt(net_usdt: float, state: RecoveryState) -> bool:
    """True if this close would clear (or there is no) martingale debt."""
    debt = max(0.0, state.cumulative_loss_usdt)
    return debt <= 0 or net_usdt >= debt


def format_status(state: RecoveryState, *, lock_min: float = 0.0) -> str:
    if state.level <= 0 and state.cumulative_loss_usdt <= 0:
        return "recovery off (level 0)"
    lock = ""
    if state.locked_side:
        lock = f" · locked {state.locked_side.upper()}"
        if lock_min > 0:
            left = max(0.0, lock_min * 60.0 - side_lock_age_sec(state))
            lock += f" {left / 60.0:.0f}m left" if left > 0 else " (expired)"
    return (
        f"recovery level {state.level} ({state.multiplier:g}x){lock} · "
        f"loss streak {state.loss_streak} · cumulative {state.cumulative_loss_usdt:.4f} USDT"
    )
