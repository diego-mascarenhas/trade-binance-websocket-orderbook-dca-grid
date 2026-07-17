#!/usr/bin/env python3
"""ML autotuner — collects bars, optimizes params, restarts scalp bot."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from ob_bars import BarBuilder, depth_to_levels
from ob_ema import fetch_ema_snapshot
from ob_scalp_backfill import backfill_from_stdout
from ob_scalp_dataset import BarRecord, append_bar, bars_path, load_bars
from ob_scalp_ml import (
    HAS_SKLEARN,
    TuneParams,
    clamp_trade_params,
    load_tuned,
    random_search,
    save_models,
    save_tuned,
    train_models,
)
from ob_signals import SignalConfig, entry_signal
from orderbook_dca_grid import fetch_depth, load_env_file
from trade_sounds import pacman_available

ROOT = Path(__file__).resolve().parent


def autotune_pid_path(symbol: str) -> Path:
    p = ROOT / ".run/logs" / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "autotune.pid"


def _pid_alive(pid: int, needle: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return needle in out


def ensure_single_instance(symbol: str) -> None:
    path = autotune_pid_path(symbol)
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            if _pid_alive(pid, "ob_scalp_autotune.py"):
                print(f"Autotune already running pid={pid}", file=sys.stderr)
                sys.exit(0)
        except ValueError:
            pass
    path.write_text(str(os.getpid()), encoding="utf-8")


def log_path(symbol: str) -> Path:
    p = ROOT / ".run/logs" / symbol.upper()
    p.mkdir(parents=True, exist_ok=True)
    return p / "scalp_autotune.log"


def log(symbol: str, msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(log_path(symbol), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def pid_path(symbol: str) -> Path:
    return ROOT / ".run/logs" / symbol.upper() / "scalp.pid"


def stdout_path(symbol: str) -> Path:
    return ROOT / ".run/logs" / symbol.upper() / "scalp_stdout.log"


def read_pid(symbol: str) -> int | None:
    path = pid_path(symbol)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return None
    if not _pid_alive(pid, "orderbook_ob_scalp.py"):
        path.unlink(missing_ok=True)
        return None
    return pid


def ping_exchange(symbol: str) -> tuple[bool, str]:
    """Quick Binance connectivity check."""
    try:
        depth = fetch_depth(symbol.upper(), 5)
        if depth.get("bids") and depth.get("asks"):
            return True, "ok"
        return False, "empty order book"
    except Exception as exc:
        return False, str(exc)[:120]


def ensure_bot_alive(
    symbol: str,
    params: TuneParams,
    *,
    execute: bool,
    recover: bool,
    recover_max_level: int,
    bar_sec: float,
) -> int | None:
    """Restart trading bot if process died."""
    pid = read_pid(symbol)
    if pid is not None:
        return pid
    net_ok, net_msg = ping_exchange(symbol)
    if not net_ok:
        log(symbol, f"Bot down · network down ({net_msg}) — wait next cycle")
        return None
    pid = start_bot(
        symbol, params, execute=execute, recover=recover,
        recover_max_level=recover_max_level, bar_sec=bar_sec,
    )
    log(symbol, f"Bot was down — restarted pid={pid}")
    return pid


def health_check(symbol: str) -> None:
    net_ok, net_msg = ping_exchange(symbol)
    bot_pid = read_pid(symbol)
    if net_ok and bot_pid:
        log(symbol, f"Health OK · network · bot pid={bot_pid}")
    elif net_ok:
        log(symbol, "Health WARN · network OK · bot DOWN")
    elif bot_pid:
        log(symbol, f"Health WARN · network FAIL ({net_msg}) · bot pid={bot_pid}")
    else:
        log(symbol, f"Health FAIL · network ({net_msg}) · bot DOWN")


def stop_bot(symbol: str) -> None:
    pid = read_pid(symbol)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.5)
        except OSError:
            pass
    pid_path(symbol).unlink(missing_ok=True)


def start_bot(
    symbol: str,
    params: TuneParams,
    *,
    execute: bool,
    recover: bool,
    recover_max_level: int,
    bar_sec: float,
) -> int:
    log_dir = ROOT / ".run/logs" / symbol.upper()
    log_dir.mkdir(parents=True, exist_ok=True)
    bars_n = len(load_bars(symbol))
    tuned = clamp_trade_params(TuneParams(**params.__dict__))
    ml_cap = float(os.getenv("OB_ML_MIN_PROB", "0.25") or 0.25)
    tuned.ml_min_prob = min(tuned.ml_min_prob, ml_cap, 0.35)
    cooldown = os.getenv("OB_ENTRY_COOLDOWN_SEC", "").strip()
    if cooldown:
        try:
            tuned.entry_cooldown_sec = float(cooldown)
        except ValueError:
            pass
    cmd = [
        sys.executable, "-u", str(ROOT / "orderbook_ob_scalp.py"),
        symbol.upper(),
        "--execute" if execute else "--dry-run",
    ]
    if recover:
        cmd.extend(["--recover", "--recover-max-level", str(recover_max_level)])
        lock_min = os.getenv("OB_RECOVER_LOCK_MIN", "5").strip() or "5"
        cmd.extend(["--recover-lock-min", lock_min])
    cmd.extend(["--bar-sec", str(bar_sec), "--recv-window", "15000"])
    cmd.extend(tuned.to_cli())
    # Env can force EMA off even if autotune prefers it (more entries in chop).
    ema_env = os.getenv("OB_EMA_FILTER", "1").strip().lower()
    if ema_env in ("0", "false", "no", "off"):
        cmd = [c for c in cmd if c not in ("--ema-filter", "--no-ema-filter")]
        cmd.append("--no-ema-filter")
        log(symbol, "EMA filter OFF (OB_EMA_FILTER=0)")
    cmd.append("--adaptive")
    # Follow moves longer; pattern filter gates weak chop entries.
    cmd.extend(["--max-bars", os.getenv("OB_MAX_BARS", "12")])
    size_mode = os.getenv("SCALP_SIZE_MODE", "min").strip().lower() or "min"
    base_size = os.getenv("SCALP_BASE_SIZE", "0").strip() or "0"
    cmd.extend(["--size-mode", size_mode])
    if size_mode == "fixed" and float(base_size or 0) > 0:
        cmd.extend(["--base-size", base_size])
    if os.getenv("OB_PATTERN_FILTER", "1").strip().lower() not in ("0", "false", "no", "off"):
        cmd.append("--pattern-filter")
        # Softer pattern knobs when set in env
        body = os.getenv("OB_PATTERN_MIN_BODY_RATIO", "").strip()
        cont = os.getenv("OB_PATTERN_MIN_CONTINUITY", "").strip()
        volx = os.getenv("OB_PATTERN_MIN_VOL_RATIO", "").strip()
        if body:
            cmd.extend(["--pattern-min-body-ratio", body])
        if cont:
            cmd.extend(["--pattern-min-continuity", cont])
        if volx:
            cmd.extend(["--pattern-min-vol-ratio", volx])
    else:
        cmd.append("--no-pattern-filter")
        log(symbol, "Pattern filter OFF")
    if os.getenv("OB_IMB_FILTER", "").strip().lower() in ("1", "true", "yes", "on"):
        cmd.append("--imb-filter")
    # Multi-trigger + OB wall exits (default on via bot flags; allow env override)
    mt = os.getenv("OB_MULTI_TRIGGER", "1").strip().lower()
    if mt in ("0", "false", "no", "off"):
        cmd.append("--no-multi-trigger")
    else:
        cmd.append("--multi-trigger")
    ox = os.getenv("OB_EXITS", "1").strip().lower()
    if ox in ("0", "false", "no", "off"):
        cmd.append("--no-ob-exits")
    else:
        cmd.append("--ob-exits")
    # Keep ML on once we have a small sample; soft threshold handles low-confidence bars.
    if bars_n < 20:
        cmd.append("--no-ml-filter")
        log(symbol, f"ML filter OFF ({bars_n}/20 bars for reliable model)")
    else:
        log(symbol, f"ML filter ON min_prob={tuned.ml_min_prob:.2f} ({bars_n} bars)")
    if size_mode == "fixed":
        log(symbol, f"Size mode fixed base={base_size} USDT")

    out = open(stdout_path(symbol), "a", encoding="utf-8")
    env = os.environ.copy()
    if not env.get("OB_SOUND_PACK", "").strip() and pacman_available():
        env.setdefault("OB_SOUND_PACK", "pacman")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    pid_path(symbol).write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def bootstrap_bars(
    symbol: str,
    *,
    target: int,
    bar_sec: float,
    band_pct: float = 1.0,
    use_ema: bool = True,
    ema_slope_min: float = 0.05,
) -> int:
    """Collect live bars until dataset reaches target size."""
    existing = load_bars(symbol)
    if len(existing) >= target:
        return len(existing)

    need = target - len(existing)
    log(symbol, f"Bootstrap: collecting {need} live bars (~{need * bar_sec / 60:.0f} min)")
    sig_cfg = SignalConfig()
    builder = BarBuilder(bar_sec=bar_sec, band_pct=band_pct)
    builder.start_bar(time.time())
    collected = 0
    deadline = time.time() + bar_sec * need + 30

    while collected < need and time.time() < deadline:
        depth = fetch_depth(symbol, 100)
        bids, asks = depth_to_levels(depth)
        bar = builder.add_sample(bids, asks, time.time())
        if bar is None:
            time.sleep(2)
            continue
        ob_sig = entry_signal(bar, sig_cfg)
        ema = None
        if use_ema:
            try:
                ema = fetch_ema_snapshot(symbol, slope_min_pct=ema_slope_min)
            except Exception:
                pass
        rec = BarRecord.from_bar(bar, ob_signal=ob_sig, ema=ema)
        append_bar(symbol, rec)
        collected += 1
        log(symbol, f"  bar {collected}/{need} imb={rec.imbalance*100:.1f}% mid={rec.mid_c:.2f}")
        builder.reset_after_bar(time.time())
        time.sleep(0.5)

    return len(load_bars(symbol))


def run_tune_cycle(
    symbol: str,
    *,
    min_bars: int,
    n_iter: int,
    base: TuneParams,
) -> tuple[TuneParams, dict[str, float]]:
    bars = load_bars(symbol, limit=500)
    if len(bars) < min_bars:
        raise RuntimeError(f"Need {min_bars} bars, have {len(bars)}")

    log(symbol, f"ML tune on {len(bars)} bars (sklearn={'yes' if HAS_SKLEARN else 'no'})")
    best, stats, model = random_search(bars, n_iter=n_iter, base=base, symbol=symbol)
    if model:
        log(symbol, f"  RF cv long={model.cv_long:.2f} short={model.cv_short:.2f}")
    log(
        symbol,
        f"  best score={stats['score']:.4f} pnl={stats['pnl_sum']:.3f}% "
        f"trades={int(stats['trades'])} win={stats['win_rate']*100:.0f}%",
    )
    log(
        symbol,
        f"  params imb {best.imb_long:.3f}/{best.imb_short:.3f} "
        f"mom={best.momentum_min_pct:.3f} tp={best.tp_pct:.2f} sl={best.sl_pct:.2f} "
        f"ema_slope={best.ema_slope_min:.3f} ml_prob={best.ml_min_prob:.2f}",
    )
    save_tuned(symbol, best, stats, model)
    save_models(symbol, model)
    return best, stats


def should_apply(new_stats: dict[str, float], old_stats: dict[str, float] | None, min_delta: float) -> bool:
    if old_stats is None:
        return True
    return new_stats["score"] >= old_stats.get("score", -999) + min_delta


def main() -> None:
    p = argparse.ArgumentParser(description="ML autotuner for OB scalp")
    p.add_argument("symbol", nargs="?", default="ZECUSDT")
    p.add_argument("--interval-min", type=float, default=2.0,
                   help="Minutes between health check + ML tune cycles (default 2)")
    p.add_argument("--min-bars", type=int, default=25, help="Min bars before first tune")
    p.add_argument("--bootstrap-bars", type=int, default=None,
                   help="Live bars to collect if dataset empty (default: same as --min-bars)")
    p.add_argument("--n-iter", type=int, default=120, help="Random search iterations")
    p.add_argument("--min-delta", type=float, default=0.02, help="Min score improvement to restart bot")
    p.add_argument("--bar-sec", type=float, default=60.0)
    p.add_argument("--execute", action="store_true", help="Run bot live (default dry-run)")
    p.add_argument("--recover", action="store_true", default=True)
    p.add_argument("--no-recover", action="store_true")
    p.add_argument("--recover-max-level", type=int, default=3)
    p.add_argument("--once", action="store_true", help="Single tune cycle then exit")
    args = p.parse_args()
    load_env_file(None)

    sym = args.symbol.upper()
    ensure_single_instance(sym)
    recover = args.recover and not args.no_recover
    execute = args.execute

    tuned, meta = load_tuned(sym)
    params = tuned or TuneParams()
    old_stats = meta.get("stats") if meta else None

    log(sym, f"Autotune start execute={execute} recover={recover} interval={args.interval_min}m")

    backfilled = backfill_from_stdout(sym)
    if backfilled:
        log(sym, f"Backfilled {backfilled} bars from stdout log")

    boot_target = args.bootstrap_bars if args.bootstrap_bars is not None else args.min_bars
    if len(load_bars(sym)) < args.min_bars:
        if read_pid(sym) is None:
            bootstrap_bars(
                sym,
                target=max(boot_target, args.min_bars),
                bar_sec=args.bar_sec,
                use_ema=params.use_ema,
                ema_slope_min=params.ema_slope_min,
            )
        else:
            log(sym, f"Bot running — collecting bars ({len(load_bars(sym))}/{args.min_bars}), skip bootstrap")

    if read_pid(sym) is None:
        pid = start_bot(sym, params, execute=execute, recover=recover,
                        recover_max_level=args.recover_max_level, bar_sec=args.bar_sec)
        log(sym, f"Started bot pid={pid}")

    while True:
        try:
            health_check(sym)
            ensure_bot_alive(
                sym, params, execute=execute, recover=recover,
                recover_max_level=args.recover_max_level, bar_sec=args.bar_sec,
            )
            bars_n = len(load_bars(sym))
            if bars_n >= args.min_bars:
                new_params, new_stats = run_tune_cycle(
                    sym, min_bars=args.min_bars, n_iter=args.n_iter, base=params,
                )
                if should_apply(new_stats, old_stats, args.min_delta):
                    if read_pid(sym):
                        log(sym, "Restarting bot with tuned params")
                        stop_bot(sym)
                    pid = start_bot(
                        sym, new_params, execute=execute, recover=recover,
                        recover_max_level=args.recover_max_level, bar_sec=args.bar_sec,
                    )
                    log(sym, f"Bot restarted pid={pid}")
                    params = new_params
                    old_stats = new_stats
                else:
                    log(sym, f"No restart — delta {new_stats['score'] - (old_stats or {}).get('score', 0):.4f} < {args.min_delta}")
            else:
                log(sym, f"Waiting for bars ({bars_n}/{args.min_bars})")
        except Exception as exc:
            log(sym, f"Tune error: {exc}")

        if args.once:
            break
        time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    main()
