"""Post-close learning: was the exit good, or did the signal prosper after SL?"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ob_scalp_adaptive import AdaptiveState, apply_delta, load_adaptive, save_adaptive, trade_samples_path
from ob_scalp_recovery import append_journal

ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / ".run" / "logs"


@dataclass
class OutcomeWatch:
    watch_id: str
    symbol: str
    signal: str
    features: list[float]
    entry: float
    exit_price: float
    is_long: bool
    reason: str
    net_usdt: float
    won: bool
    tp_pct: float
    sl_pct: float
    fee_buffer: float
    started_at: float
    ends_at: float
    marks: list[float] = field(default_factory=list)


def _pending_path(symbol: str) -> Path:
    p = LOG_ROOT / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "scalp_outcome_pending.json"


def _learn_log_path(symbol: str) -> Path:
    p = LOG_ROOT / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "scalp_learn.jsonl"


def _load_pending(symbol: str) -> list[OutcomeWatch]:
    path = _pending_path(symbol)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    out: list[OutcomeWatch] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        out.append(
            OutcomeWatch(
                watch_id=str(item.get("watch_id", "")),
                symbol=str(item.get("symbol", symbol)).upper(),
                signal=str(item.get("signal", "")),
                features=list(item.get("features") or []),
                entry=float(item.get("entry", 0)),
                exit_price=float(item.get("exit_price", 0)),
                is_long=bool(item.get("is_long", True)),
                reason=str(item.get("reason", "")),
                net_usdt=float(item.get("net_usdt", 0)),
                won=bool(item.get("won", False)),
                tp_pct=float(item.get("tp_pct", 0.3)),
                sl_pct=float(item.get("sl_pct", 0.12)),
                fee_buffer=float(item.get("fee_buffer", 0.08)),
                started_at=float(item.get("started_at", 0)),
                ends_at=float(item.get("ends_at", 0)),
                marks=[float(x) for x in (item.get("marks") or [])],
            )
        )
    return out


def _save_pending(symbol: str, watches: list[OutcomeWatch]) -> None:
    _pending_path(symbol).write_text(
        json.dumps([asdict(w) for w in watches], indent=2) + "\n",
        encoding="utf-8",
    )


def _append_learn_log(symbol: str, record: dict[str, Any]) -> None:
    with open(_learn_log_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _favorable_from_entry(entry: float, is_long: bool, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if is_long:
        return (price - entry) / entry * 100
    return (entry - price) / entry * 100


def classify_outcome(watch: OutcomeWatch) -> tuple[str, int, float]:
    """Return verdict, ML entry label (0/1), best favorable move from entry %."""
    prices = [watch.exit_price, *watch.marks]
    if watch.is_long:
        best = max(prices)
    else:
        best = min(prices)
    move = _favorable_from_entry(watch.entry, watch.is_long, best)

    if watch.won:
        if move >= watch.tp_pct * 0.85:
            return "tp_good", 1, move
        if move >= watch.tp_pct * 0.5:
            return "tp_early", 1, move
        return "tp_weak_follow", 1, move

    if watch.reason in ("SL", "TRAIL"):
        if move >= watch.tp_pct * 0.7:
            return "premature_sl", 1, move
        if move >= watch.sl_pct:
            return "signal_ok_sl_tight", 1, move
        return "sl_correct", 0, move

    return "loss", 0, move


def _apply_verdict(state: AdaptiveState, verdict: str) -> None:
    if verdict == "premature_sl":
        apply_delta(state, {"ml_min_prob": -0.03, "ema_slope_min": -0.003, "sl_pct_adj": 0.04})
    elif verdict == "signal_ok_sl_tight":
        apply_delta(state, {"ml_min_prob": -0.015, "sl_pct_adj": 0.025})
    elif verdict == "sl_correct":
        apply_delta(state, {"ml_min_prob": 0.02, "ema_slope_min": 0.003})
    elif verdict in ("tp_good", "tp_early"):
        apply_delta(state, {"ml_min_prob": 0.01})
    elif verdict == "tp_weak_follow":
        apply_delta(state, {"ml_min_prob": 0.005})


def register_close_watch(
    symbol: str,
    *,
    signal: str,
    features: list[float],
    entry: float,
    exit_price: float,
    is_long: bool,
    reason: str,
    net_usdt: float,
    tp_pct: float,
    sl_pct: float,
    fee_buffer: float,
    watch_sec: float,
) -> str:
    """Start post-close observation window (default ~3 bars)."""
    sym = symbol.upper()
    watch = OutcomeWatch(
        watch_id=uuid.uuid4().hex[:8],
        symbol=sym,
        signal=signal.lower(),
        features=features,
        entry=entry,
        exit_price=exit_price,
        is_long=is_long,
        reason=reason.upper(),
        net_usdt=net_usdt,
        won=net_usdt > 0,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        fee_buffer=fee_buffer,
        started_at=time.time(),
        ends_at=time.time() + watch_sec,
        marks=[],
    )
    pending = _load_pending(sym)
    pending.append(watch)
    _save_pending(sym, pending)
    append_journal(
        sym,
        f"LEARN watch {watch.watch_id} {reason} — observing {watch_sec / 60:.1f}m post-close",
    )
    return (
        f"Learn watch {watch_sec / 60:.1f}m after {reason} "
        f"({signal.upper()} pnl={net_usdt:+.4f})"
    )


def finalize_watch(watch: OutcomeWatch, state: AdaptiveState) -> str:
    verdict, label, best_move = classify_outcome(watch)
    state.last_trade_at = watch.started_at
    state.trades += 1
    if label == 1 and watch.won:
        state.wins += 1
    elif label == 0 and not watch.won:
        state.losses += 1
    elif label == 1 and not watch.won:
        pass  # premature SL — signal good, exit bad; don't count as loss streak mentally
    else:
        state.losses += 1

    _apply_verdict(state, verdict)
    save_adaptive(watch.symbol, state)

    sample = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal": watch.signal,
        "features": watch.features,
        "label": label,
        "verdict": verdict,
        "best_move_pct": round(best_move, 4),
        "net_usdt": round(watch.net_usdt, 6),
        "reason": watch.reason,
    }
    with open(trade_samples_path(watch.symbol), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "watch_id": watch.watch_id,
        "verdict": verdict,
        "label": label,
        "best_move_pct": round(best_move, 4),
        "reason": watch.reason,
        "signal": watch.signal,
        "net_usdt": round(watch.net_usdt, 6),
        "entry": watch.entry,
        "exit": watch.exit_price,
        "marks_n": len(watch.marks),
    }
    _append_learn_log(watch.symbol, record)
    append_journal(
        watch.symbol,
        f"LEARN {verdict} label={label} best_move={best_move:+.3f}% "
        f"after {watch.reason} pnl={watch.net_usdt:+.4f}",
    )

    verdict_human = {
        "premature_sl": "SL prematuro — la señal prosperó después",
        "signal_ok_sl_tight": "Señal OK — SL demasiado ajustado",
        "sl_correct": "SL correcto — precio siguió en contra",
        "tp_good": "TP acertado — buen seguimiento",
        "tp_early": "TP temprano — aún había recorrido",
        "tp_weak_follow": "TP OK — poco follow-through",
        "loss": "Cierre perdedor",
    }.get(verdict, verdict)

    sl_adj = getattr(state, "sl_pct_adj", 0.0)
    return (
        f"Learn {verdict}: {verdict_human} · move={best_move:+.3f}% · "
        f"ml={state.ml_min_prob:.2f} sl_adj=+{sl_adj:.3f}%"
    )


def tick_outcome_watches(symbol: str, mark: float, now: float | None = None) -> list[str]:
    """Sample mark prices; finalize watches when window ends. Returns log lines."""
    now = now or time.time()
    sym = symbol.upper()
    pending = _load_pending(sym)
    if not pending:
        return []

    messages: list[str] = []
    still: list[OutcomeWatch] = []
    state = load_adaptive(sym)

    for watch in pending:
        if mark > 0:
            watch.marks.append(mark)
        if now < watch.ends_at:
            still.append(watch)
            continue
        messages.append(finalize_watch(watch, state))
        state = load_adaptive(sym)

    _save_pending(sym, still)
    return messages


def effective_sl_pct(state: AdaptiveState, base_sl: float) -> float:
    adj = float(getattr(state, "sl_pct_adj", 0.0) or 0.0)
    return min(0.50, base_sl + adj)
