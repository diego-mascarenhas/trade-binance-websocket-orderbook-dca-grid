"""ML-backed parameter search for OB scalp (backtest + RandomForest signal filter)."""

from __future__ import annotations

import json
import math
import random
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ob_bars import OBBar
from ob_scalp_dataset import BarRecord, load_bars
from ob_signals import SignalConfig, entry_signal, profit_pct, should_tp_close

try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


@dataclass
class TuneParams:
    imb_long: float = 0.58
    imb_short: float = 0.42
    momentum_min_pct: float = 0.02
    tp_pct: float = 0.30
    sl_pct: float = 0.12
    ema_slope_min: float = 0.05
    entry_cooldown_sec: float = 60.0
    fee_buffer: float = 0.08
    use_ema: bool = True
    ml_min_prob: float = 0.48

    def to_cli(self) -> list[str]:
        args = [
            "--imb-long", f"{self.imb_long:.3f}",
            "--imb-short", f"{self.imb_short:.3f}",
            "--momentum-min-pct", f"{self.momentum_min_pct:.3f}",
            "--tp-pct", f"{self.tp_pct:.2f}",
            "--sl-pct", f"{self.sl_pct:.2f}",
            "--entry-cooldown-sec", f"{self.entry_cooldown_sec:.0f}",
            "--ema-slope-min", f"{self.ema_slope_min:.3f}",
        ]
        if self.use_ema:
            args.append("--ema-filter")
        else:
            args.append("--no-ema-filter")
        args.extend(["--ml-min-prob", f"{self.ml_min_prob:.2f}"])
        return args


def _record_to_bar(rec: BarRecord) -> OBBar:
    return OBBar(
        t_open=rec.t_close - 60,
        t_close=rec.t_close,
        mid_o=rec.mid_o,
        mid_h=rec.mid_h,
        mid_l=rec.mid_l,
        mid_c=rec.mid_c,
        spread_avg=rec.spread_avg,
        imbalance=rec.imbalance,
        bid_vol=rec.bid_vol,
        ask_vol=rec.ask_vol,
        bid_wall_price=0.0,
        bid_wall_qty=rec.bid_wall_qty,
        ask_wall_price=0.0,
        ask_wall_qty=rec.ask_wall_qty,
        samples=1,
    )


def feature_vector(rec: BarRecord) -> list[float]:
    return [
        rec.imbalance,
        rec.mid_chg_pct,
        rec.spread_avg / rec.mid_c * 100 if rec.mid_c else 0.0,
        math.log1p(rec.bid_wall_qty),
        math.log1p(rec.ask_wall_qty),
        rec.ema_slope_pct or 0.0,
        1.0 if rec.ema_trend == "bullish" else 0.0,
        1.0 if rec.ema_trend == "bearish" else 0.0,
        rec.bid_vol / max(rec.ask_vol, 1e-9),
    ]


def forward_long_pnl(rec: BarRecord, nxt: BarRecord, tp_pct: float, sl_pct: float) -> float:
    entry = rec.mid_c
    if entry <= 0:
        return 0.0
    hi = max(nxt.mid_h, nxt.mid_c)
    lo = min(nxt.mid_l, nxt.mid_c)
    tp_px = entry * (1 + tp_pct / 100)
    sl_px = entry * (1 - sl_pct / 100)
    if lo <= sl_px:
        return profit_pct(entry, sl_px, True)
    if hi >= tp_px:
        return profit_pct(entry, tp_px, True)
    return profit_pct(entry, nxt.mid_c, True)


def forward_short_pnl(rec: BarRecord, nxt: BarRecord, tp_pct: float, sl_pct: float) -> float:
    entry = rec.mid_c
    if entry <= 0:
        return 0.0
    hi = max(nxt.mid_h, nxt.mid_c)
    lo = min(nxt.mid_l, nxt.mid_c)
    tp_px = entry * (1 - tp_pct / 100)
    sl_px = entry * (1 + sl_pct / 100)
    if hi >= sl_px:
        return profit_pct(entry, sl_px, False)
    if lo <= tp_px:
        return profit_pct(entry, tp_px, False)
    return profit_pct(entry, nxt.mid_c, False)


def build_training_matrix(
    bars: list[BarRecord],
    *,
    tp_pct: float = 0.30,
    sl_pct: float = 0.12,
    fee_buffer: float = 0.08,
) -> tuple[list[list[float]], list[int], list[int]]:
    """Features + labels: 1 if forward net PnL > 0 for long/short."""
    xs: list[list[float]] = []
    y_long: list[int] = []
    y_short: list[int] = []
    for i in range(len(bars) - 1):
        rec, nxt = bars[i], bars[i + 1]
        xs.append(feature_vector(rec))
        lp = forward_long_pnl(rec, nxt, tp_pct, sl_pct) - fee_buffer
        sp = forward_short_pnl(rec, nxt, tp_pct, sl_pct) - fee_buffer
        y_long.append(1 if lp > 0 else 0)
        y_short.append(1 if sp > 0 else 0)
    return xs, y_long, y_short


@dataclass
class SignalModel:
    long_clf: Any
    short_clf: Any
    cv_long: float
    cv_short: float


def train_models(bars: list[BarRecord], params: TuneParams, *, symbol: str = "") -> SignalModel | None:
    if not HAS_SKLEARN or len(bars) < 15:
        return None
    xs, y_long, y_short = build_training_matrix(
        bars, tp_pct=params.tp_pct, sl_pct=params.sl_pct, fee_buffer=params.fee_buffer,
    )
    if symbol:
        try:
            from ob_scalp_adaptive import load_trade_samples

            for sample in load_trade_samples(symbol):
                fv = sample.get("features")
                sig = str(sample.get("signal", "")).lower()
                if not isinstance(fv, list) or len(fv) != 9:
                    continue
                label = int(sample.get("label", 0))
                xs.append(fv)
                if sig == "long":
                    y_long.append(label)
                    y_short.append(0)
                elif sig == "short":
                    y_long.append(0)
                    y_short.append(label)
        except ImportError:
            pass
    if len(xs) < 20:
        return None
    x = np.array(xs)
    long_clf = RandomForestClassifier(n_estimators=80, max_depth=5, random_state=42, n_jobs=1)
    short_clf = RandomForestClassifier(n_estimators=80, max_depth=5, random_state=43, n_jobs=1)
    long_clf.fit(x, y_long)
    short_clf.fit(x, y_short)
    cv_folds = min(5, len(xs) // 4)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module=r"sklearn\.")
        cv_l = float(cross_val_score(long_clf, x, y_long, cv=cv_folds, scoring="accuracy").mean())
        cv_s = float(cross_val_score(short_clf, x, y_short, cv=cv_folds, scoring="accuracy").mean())
    return SignalModel(long_clf=long_clf, short_clf=short_clf, cv_long=cv_l, cv_short=cv_s)


def ema_allows_record(signal: str, rec: BarRecord, slope_min: float) -> bool:
    if rec.ema_trend is None:
        return True
    if signal == "long":
        return bool(rec.ema_allow_long)
    if signal == "short":
        return bool(rec.ema_allow_short)
    return True


def backtest(
    bars: list[BarRecord],
    params: TuneParams,
    model: SignalModel | None = None,
) -> dict[str, float]:
    cfg = SignalConfig(
        imb_long=params.imb_long,
        imb_short=params.imb_short,
        require_momentum=True,
        momentum_min_pct=params.momentum_min_pct,
    )
    pos_side: str | None = None
    entry = 0.0
    last_close_t = 0.0
    trades = 0
    wins = 0
    pnl_sum = 0.0

    for rec in bars:
        bar = _record_to_bar(rec)
        mark = rec.mid_c

        if pos_side is not None:
            is_long = pos_side == "long"
            pnl = profit_pct(entry, mark, is_long)
            closed = False
            if should_tp_close(pnl, params.tp_pct, params.fee_buffer):
                net = pnl - params.fee_buffer
                pnl_sum += net
                wins += 1 if net > 0 else 0
                trades += 1
                closed = True
            elif pnl <= -params.sl_pct:
                net = pnl - params.fee_buffer
                pnl_sum += net
                trades += 1
                closed = True
            if closed:
                pos_side = None
                last_close_t = rec.t_close
                continue

        if pos_side is not None:
            continue

        if last_close_t and (rec.t_close - last_close_t) < params.entry_cooldown_sec:
            continue

        sig = entry_signal(bar, cfg)
        if not sig:
            continue
        if params.use_ema and not ema_allows_record(sig, rec, params.ema_slope_min):
            continue
        if model and HAS_SKLEARN:
            fv = np.array([feature_vector(rec)])
            prob = (
                float(model.long_clf.predict_proba(fv)[0][1])
                if sig == "long"
                else float(model.short_clf.predict_proba(fv)[0][1])
            )
            if prob < params.ml_min_prob:
                continue

        pos_side = sig
        entry = mark

    win_rate = wins / trades if trades else 0.0
    score = pnl_sum + win_rate * 0.05
    return {
        "score": score,
        "pnl_sum": pnl_sum,
        "trades": float(trades),
        "wins": float(wins),
        "win_rate": win_rate,
    }


def random_search(
    bars: list[BarRecord],
    *,
    n_iter: int = 120,
    seed: int = 42,
    base: TuneParams | None = None,
    symbol: str = "",
) -> tuple[TuneParams, dict[str, float], SignalModel | None]:
    rng = random.Random(seed)
    base = base or TuneParams()
    best_params = base
    best_stats = backtest(bars, base)
    best_model: SignalModel | None = None

    model = train_models(bars, base, symbol=symbol)

    for _ in range(n_iter):
        cand = TuneParams(
            imb_long=round(rng.uniform(0.54, 0.65), 3),
            imb_short=round(rng.uniform(0.35, 0.46), 3),
            momentum_min_pct=round(rng.uniform(0.005, 0.04), 3),
            tp_pct=round(rng.uniform(0.18, 0.45), 2),
            sl_pct=round(rng.uniform(0.08, 0.18), 2),
            ema_slope_min=round(rng.uniform(0.02, 0.12), 3),
            entry_cooldown_sec=rng.choice([45.0, 60.0, 90.0]),
            fee_buffer=base.fee_buffer,
            use_ema=rng.random() > 0.15,
            ml_min_prob=round(rng.uniform(0.42, 0.52), 2),
        )
        if cand.imb_long <= cand.imb_short + 0.08:
            continue
        if cand.tp_pct <= cand.sl_pct:
            continue
        stats = backtest(bars, cand, model=model)
        if stats["score"] > best_stats["score"]:
            best_stats = stats
            best_params = cand
            best_model = model

    refined = TuneParams(**asdict(best_params))
    for attr, delta in [
        ("imb_long", 0.01),
        ("imb_short", 0.01),
        ("momentum_min_pct", 0.005),
        ("tp_pct", 0.02),
        ("sl_pct", 0.01),
        ("ema_slope_min", 0.01),
    ]:
        for sign in (-1, 1):
            trial = TuneParams(**asdict(refined))
            val = getattr(trial, attr) + sign * delta
            setattr(trial, attr, round(val, 4))
            if trial.imb_long <= trial.imb_short + 0.08 or trial.tp_pct <= trial.sl_pct:
                continue
            stats = backtest(bars, trial, model=model)
            if stats["score"] > best_stats["score"]:
                best_stats = stats
                refined = trial

    return refined, best_stats, model


def tuned_config_path(symbol: str) -> Path:
    path = Path(".run/logs") / symbol.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path / "scalp_tuned.json"


def model_path(symbol: str) -> Path:
    return Path(".run/logs") / symbol.upper() / "scalp_model.pkl"


def save_models(symbol: str, model: SignalModel | None) -> None:
    if model is None:
        return
    import pickle

    with open(model_path(symbol), "wb") as fh:
        pickle.dump(model, fh)


def load_models(symbol: str) -> SignalModel | None:
    if not HAS_SKLEARN:
        return None
    path = model_path(symbol)
    if not path.exists():
        return None
    import pickle

    try:
        return pickle.load(open(path, "rb"))
    except Exception:
        return None


def predict_prob(model: SignalModel, rec: BarRecord, signal: str) -> float:
    if not HAS_SKLEARN:
        return 1.0
    fv = np.array([feature_vector(rec)])
    clf = model.long_clf if signal == "long" else model.short_clf
    return float(clf.predict_proba(fv)[0][1])


def save_tuned(symbol: str, params: TuneParams, stats: dict[str, float], model: SignalModel | None) -> None:
    payload = {
        "params": asdict(params),
        "stats": stats,
        "ml": {
            "sklearn": HAS_SKLEARN,
            "cv_long": model.cv_long if model else None,
            "cv_short": model.cv_short if model else None,
        },
    }
    tuned_config_path(symbol).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_tuned(symbol: str) -> tuple[TuneParams | None, dict[str, Any]]:
    path = tuned_config_path(symbol)
    if not path.exists():
        return None, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return TuneParams(**data["params"]), data
