"""Accumulated PnL tracking for OB scalp per symbol."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ob_scalp_recovery import journal_path

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"

_CLOSE_RE = re.compile(
    r"(?:TP|SL|TRAIL|FLIP|MAXBARS) (LONG|SHORT).*?pnl=([+-]?\d+\.?\d*) USDT",
)
_START_RE = re.compile(r"START ")


@dataclass
class PnlStats:
    symbol: str
    session_pnl: float = 0.0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    trades: int = 0
    session_wins: int = 0
    session_losses: int = 0
    updated_at: str = ""

    @property
    def session_trades(self) -> int:
        return self.session_wins + self.session_losses


def pnl_path(symbol: str) -> Path:
    path = LOG_ROOT / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path / "scalp_pnl.json"


def compute_from_journal(symbol: str) -> PnlStats:
    sym = symbol.upper()
    stats = PnlStats(symbol=sym)
    path = journal_path(sym)
    if not path.exists():
        return stats

    session_pnl = 0.0
    session_wins = 0
    session_losses = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        if _START_RE.search(line):
            session_pnl = 0.0
            session_wins = 0
            session_losses = 0
            continue
        m = _CLOSE_RE.search(line)
        if not m:
            continue
        pnl = float(m.group(2))
        stats.trades += 1
        stats.total_pnl += pnl
        session_pnl += pnl
        if pnl > 0:
            stats.wins += 1
            session_wins += 1
        else:
            stats.losses += 1
            session_losses += 1

    stats.session_pnl = session_pnl
    stats.session_wins = session_wins
    stats.session_losses = session_losses
    stats.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return stats


def save_stats(stats: PnlStats) -> None:
    pnl_path(stats.symbol).write_text(
        json.dumps(asdict(stats), indent=2) + "\n",
        encoding="utf-8",
    )


def load_pnl_stats(symbol: str, *, recompute: bool = False) -> PnlStats:
    sym = symbol.upper()
    path = pnl_path(sym)
    if not recompute and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("symbol") == sym:
                return PnlStats(
                    symbol=sym,
                    session_pnl=float(raw.get("session_pnl", 0) or 0),
                    total_pnl=float(raw.get("total_pnl", 0) or 0),
                    wins=int(raw.get("wins", 0) or 0),
                    losses=int(raw.get("losses", 0) or 0),
                    trades=int(raw.get("trades", 0) or 0),
                    session_wins=int(raw.get("session_wins", 0) or 0),
                    session_losses=int(raw.get("session_losses", 0) or 0),
                    updated_at=str(raw.get("updated_at", "") or ""),
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    stats = compute_from_journal(sym)
    save_stats(stats)
    return stats


def refresh_pnl_stats(symbol: str) -> PnlStats:
    stats = compute_from_journal(symbol)
    save_stats(stats)
    return stats


def format_pnl_line(
    stats: PnlStats,
    *,
    unrealized_usdt: float | None = None,
    compact: bool = False,
) -> str:
    from orderbook_dca_grid import DIM, GREEN, RED, RESET, YELLOW

    sym = stats.symbol
    s_color = GREEN if stats.session_pnl >= 0 else RED
    t_color = GREEN if stats.total_pnl >= 0 else RED
    wl = f"{stats.session_wins}W/{stats.session_losses}L"

    if compact:
        base = (
            f"{sym} session {s_color}{stats.session_pnl:+.4f}{RESET} USDT "
            f"({wl}) · total {t_color}{stats.total_pnl:+.4f}{RESET} USDT"
        )
    else:
        base = (
            f"{YELLOW}PnL {sym}{RESET}  "
            f"session {s_color}{stats.session_pnl:+.4f}{RESET} USDT  "
            f"{wl} · {stats.session_trades} trades  |  "
            f"total {t_color}{stats.total_pnl:+.4f}{RESET} USDT  "
            f"{stats.wins}W/{stats.losses}L"
        )
    if unrealized_usdt is not None:
        u_color = GREEN if unrealized_usdt >= 0 else RED
        base += f"  {DIM}|{RESET}  open {u_color}{unrealized_usdt:+.4f}{RESET} USDT"
    return base


def format_pnl_plain(stats: PnlStats, *, unrealized_usdt: float | None = None) -> str:
    """Plain text for session log (no ANSI)."""
    wl = f"{stats.session_wins}W/{stats.session_losses}L"
    line = (
        f"{stats.symbol} session {stats.session_pnl:+.4f} USDT ({wl}) · "
        f"total {stats.total_pnl:+.4f} USDT {stats.wins}W/{stats.losses}L"
    )
    if unrealized_usdt is not None:
        line += f" · open {unrealized_usdt:+.4f} USDT"
    return line
