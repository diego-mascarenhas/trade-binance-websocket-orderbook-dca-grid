"""Orthogonal ATH stop-loss addon (SHORT): full-size SL above historical ATH.

Replaces the old risk-reduce partial cut (RR). Behaviour:

  · One STOP_MARKET BUY reduce-only on the full position (tag RF)
  · Trigger = historical ATH × (1 + RISK_ATH_SL_PCT/100)  (default ATH + 2%)
  · New SHORT opens are blocked when price sits **below** the prior swing
    in the lookback window and within RISK_ATH_ENTRY_MIN_GAP_PCT of it
    (default 12%). The current 90d / regime high is **not** an entry block
    (that would fight ★ near the pump top).

Historical ATH (all available 1d history) is used **only** for the SL.
A 2023 print at 17.3 must not hide a 0.163 structure high.

Env / CLI:
  RISK_REDUCE=1                     # master switch (default on)
  RISK_ATH_SL_PCT=2                 # SL = ATH + this %%
  RISK_ATH_ENTRY_MIN_GAP_PCT=12     # block new opens closer than this %% below prior swing
  RISK_ATH_PRIOR=1                  # (cluster knobs still used to find that swing)
  RISK_ATH_LOOKBACK_BARS=90         # 1d bars for the entry-gate peaks
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

TAG_PARTIAL = "RR"  # legacy — cancelled on sync, never re-placed
TAG_FULL = "RF"
DEFAULT_ATH_SL_PCT = 2.0
DEFAULT_ENTRY_MIN_GAP_PCT = 12.0
DEFAULT_PRIOR_RECENT_BARS = 7  # unused; kept so old env lines still parse
DEFAULT_PRIOR_CLUSTER_PCT = 15.0
DEFAULT_PRIOR_MIN_FRAC = 0.30  # ignore leftover chop (prior must be ≥ 30% of ATH)
DEFAULT_ATH_LOOKBACK_BARS = 90
ATH_MAX_BARS = 6000  # ~16y of daily bars (paginated 1500/req)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None and getattr(args, "risk_reduce", None) is not None:
        return bool(args.risk_reduce)
    return _env_bool("RISK_REDUCE", True)


def ath_sl_pct(args: argparse.Namespace | None = None) -> float:
    """%% above ATH for the full-size stop trigger."""
    if args is not None:
        v = getattr(args, "risk_ath_sl_pct", None)
        if v is not None:
            return max(0.05, float(v))
        # Back-compat: old RISK_FULL_BUFFER_PCT / --risk-full-buffer-pct
        legacy = getattr(args, "risk_full_buffer_pct", None)
        if legacy is not None and float(legacy) > 0:
            return max(0.05, float(legacy))
    env = os.getenv("RISK_ATH_SL_PCT")
    if env is not None and str(env).strip() != "":
        try:
            return max(0.05, float(env))
        except (TypeError, ValueError):
            pass
    return max(0.05, _env_float("RISK_ATH_SL_PCT", DEFAULT_ATH_SL_PCT))


def ath_entry_min_gap_pct(args: argparse.Namespace | None = None) -> float:
    """Block new SHORT opens when distance-to-prior-swing %% is below this (default 12)."""
    if args is not None:
        v = getattr(args, "risk_ath_entry_min_gap_pct", None)
        if v is not None:
            return max(0.0, float(v))
    return max(0.0, _env_float("RISK_ATH_ENTRY_MIN_GAP_PCT", DEFAULT_ENTRY_MIN_GAP_PCT))


def prior_ath_enabled(args: argparse.Namespace | None = None) -> bool:
    if args is not None:
        v = getattr(args, "risk_ath_prior", None)
        if v is not None:
            return bool(v)
    return _env_bool("RISK_ATH_PRIOR", True)


def prior_ath_recent_bars(args: argparse.Namespace | None = None) -> int:
    if args is not None:
        v = getattr(args, "risk_ath_prior_recent_bars", None)
        if v is not None:
            return max(1, int(v))
    return max(1, int(_env_float("RISK_ATH_PRIOR_RECENT_BARS", DEFAULT_PRIOR_RECENT_BARS)))


def prior_ath_cluster_pct(args: argparse.Namespace | None = None) -> float:
    if args is not None:
        v = getattr(args, "risk_ath_prior_cluster_pct", None)
        if v is not None:
            return max(0.5, float(v))
    return max(0.5, _env_float("RISK_ATH_PRIOR_CLUSTER_PCT", DEFAULT_PRIOR_CLUSTER_PCT))


def ath_lookback_bars(args: argparse.Namespace | None = None) -> int:
    if args is not None:
        v = getattr(args, "risk_ath_lookback_bars", None)
        if v is not None:
            return max(10, int(v))
    return max(10, int(_env_float("RISK_ATH_LOOKBACK_BARS", DEFAULT_ATH_LOOKBACK_BARS)))


def allow_dca_rearm(symbol: str) -> bool:
    """Legacy hook — always allow (partial RR path removed)."""
    return True


def recovery_pct_for(
    symbol: str,
    qty: float | None = None,
    entry: float | None = None,
) -> float:
    """%% the runner must make to cover realized debt from prior reduces."""
    try:
        from exits.recovery import recovery_pct

        return float(recovery_pct(symbol, qty=qty, entry=entry) or 0)
    except Exception:
        return 0.0


def _ath_cache_path(symbol: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / ".state" / "ath" / f"{symbol.upper()}.json"


def split_ath_and_prior(
    highs: list[float],
    *,
    recent_bars: int = DEFAULT_PRIOR_RECENT_BARS,
    cluster_pct: float = DEFAULT_PRIOR_CLUSTER_PCT,
    min_frac: float = DEFAULT_PRIOR_MIN_FRAC,
) -> tuple[float | None, float | None]:
    """Return (historical ATH, previous peak or None).

    Prior = max 1d high **outside** the current ATH impulse (bars with
    high ≥ ATH×(1−cluster) around the ATH bar). ``recent_bars`` is ignored;
    the previous peak is used whenever it is a real top (≥ ``min_frac`` of ATH).
    """
    del recent_bars  # kept for call-site compat
    clean = [float(h) for h in highs if h and float(h) > 0]
    if not clean:
        return None, None
    ath = max(clean)
    floor = ath * (1.0 - max(0.0, float(cluster_pct)) / 100.0)
    ath_idx = max(i for i, h in enumerate(clean) if h >= ath * 0.9999)
    left = ath_idx
    while left > 0 and clean[left - 1] >= floor:
        left -= 1
    right = ath_idx
    while right + 1 < len(clean) and clean[right + 1] >= floor:
        right += 1
    outside = [h for i, h in enumerate(clean) if i < left or i > right]
    if not outside:
        return ath, None
    prior = max(outside)
    if prior <= 0 or prior >= ath * 0.999:
        return ath, None
    if prior < ath * max(0.0, float(min_frac)):
        return ath, None
    return ath, prior


def fetch_daily_highs(symbol: str, *, max_bars: int = ATH_MAX_BARS) -> list[float]:
    """Chronological 1d highs (oldest → newest), paginated."""
    try:
        from futures_scan import FAPI_BASE, _get
    except Exception:
        return []

    sym = symbol.upper()
    pages: list[list[float]] = []
    end_time: int | None = None
    fetched = 0
    while fetched < max_bars:
        params: dict[str, Any] = {
            "symbol": sym,
            "interval": "1d",
            "limit": 1500,
        }
        if end_time is not None:
            params["endTime"] = int(end_time)
        try:
            q = urllib.parse.urlencode(params)
            data = _get(f"{FAPI_BASE}/fapi/v1/klines?{q}")
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        highs: list[float] = []
        for row in data:
            try:
                highs.append(float(row[2]))
            except (TypeError, ValueError, IndexError):
                continue
        if not highs:
            break
        pages.append(highs)
        fetched += len(data)
        try:
            first_open = int(data[0][0])
        except (TypeError, ValueError, IndexError):
            break
        if len(data) < 1500:
            break
        end_time = first_open - 1
        if end_time <= 0:
            break
    out: list[float] = []
    for page in reversed(pages):
        out.extend(page)
    return out


def fetch_ath_bundle(
    symbol: str,
    *,
    max_bars: int = ATH_MAX_BARS,
    lookback: int = DEFAULT_ATH_LOOKBACK_BARS,
    cluster_pct: float = DEFAULT_PRIOR_CLUSTER_PCT,
) -> tuple[float | None, float | None, float | None]:
    """(historical ATH, regime ATH, prior peak) from 1d history."""
    highs = fetch_daily_highs(symbol, max_bars=max_bars)
    if not highs:
        return None, None, None
    hist = max(highs)
    n = max(10, int(lookback))
    window = highs[-n:] if len(highs) > n else highs
    regime, prior = split_ath_and_prior(window, cluster_pct=cluster_pct)
    return hist, regime, prior


def fetch_ath_levels(
    symbol: str,
    *,
    max_bars: int = ATH_MAX_BARS,
    recent_bars: int = DEFAULT_PRIOR_RECENT_BARS,
    cluster_pct: float = DEFAULT_PRIOR_CLUSTER_PCT,
    lookback: int = DEFAULT_ATH_LOOKBACK_BARS,
) -> tuple[float | None, float | None]:
    """(regime ATH, prior peak) for the entry gate — last ``lookback`` days."""
    del recent_bars
    _hist, regime, prior = fetch_ath_bundle(
        symbol, max_bars=max_bars, lookback=lookback, cluster_pct=cluster_pct,
    )
    return regime, prior


def cached_ath_levels(
    symbol: str,
    *,
    last: float | None = None,
    max_bars: int = ATH_MAX_BARS,
    ttl_s: float = 6 * 3600.0,
    args: argparse.Namespace | None = None,
) -> tuple[float | None, float | None]:
    """Cached (regime ATH, prior peak) for the entry gate."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None, None
    path = _ath_cache_path(sym)
    cached_hist: float | None = None
    cached_regime: float | None = None
    cached_prior: float | None = None
    age = 1e18
    has_regime_key = False
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached_hist = float(data.get("ath") or 0) or None
            raw_r = data.get("regime_ath")
            cached_regime = float(raw_r) if raw_r not in (None, "", 0, 0.0) else None
            has_regime_key = "regime_ath" in data
            raw_p = data.get("prior_ath")
            cached_prior = float(raw_p) if raw_p not in (None, "", 0, 0.0) else None
            age = time.time() - float(data.get("ts") or 0)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached_hist = None
    peak_for_new = cached_regime or cached_hist
    if peak_for_new and last and last > peak_for_new * 1.0001:
        cached_regime = None
    if cached_regime and cached_regime > 0 and age < ttl_s and has_regime_key:
        return cached_regime, cached_prior
    hist, regime, prior = fetch_ath_bundle(
        sym,
        max_bars=max_bars,
        lookback=ath_lookback_bars(args),
        cluster_pct=prior_ath_cluster_pct(args),
    )
    if regime and regime > 0:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "symbol": sym,
                    "ath": hist,
                    "regime_ath": regime,
                    "prior_ath": prior,
                    "ts": time.time(),
                }) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return regime, prior
    return cached_regime, cached_prior


def cached_historical_ath(
    symbol: str,
    *,
    last: float | None = None,
    max_bars: int = ATH_MAX_BARS,
    ttl_s: float = 6 * 3600.0,
) -> float | None:
    """Historical ATH (SL). Prefers cache written by ``cached_ath_levels``."""
    path = _ath_cache_path(symbol)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hist = float(data.get("ath") or 0) or None
            age = time.time() - float(data.get("ts") or 0)
            if hist and hist > 0 and age < ttl_s:
                if last and last > hist * 1.0001:
                    hist = None
                else:
                    return hist
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return fetch_historical_ath(symbol, max_bars=max_bars)


def fetch_historical_ath(symbol: str, *, max_bars: int = ATH_MAX_BARS) -> float | None:
    """Max daily high over available Binance futures history (paginated)."""
    highs = fetch_daily_highs(symbol, max_bars=max_bars)
    return max(highs) if highs else None


# Back-compat alias used by older call sites / Help page
def fetch_impulse_high(symbol: str, *, bars: int = 1500) -> float | None:
    return fetch_historical_ath(symbol, max_bars=max(1500, int(bars)))


def distance_to_ath_pct(price: float, ath: float) -> float:
    """How far ``price`` sits below ATH as %% of ATH (0 = at/above ATH)."""
    if ath <= 0 or price <= 0:
        return 0.0
    return max(0.0, (ath - price) / ath * 100.0)


def ath_sl_trigger(ath: float, args: argparse.Namespace | None = None) -> float:
    return float(ath) * (1.0 + ath_sl_pct(args) / 100.0)


def entry_blocked_near_ath(
    symbol: str,
    price: float,
    args: argparse.Namespace | None = None,
    *,
    ath: float | None = None,
    prior_ath: float | None = None,
) -> tuple[bool, str]:
    """Return (blocked, reason) for a new SHORT open near the prior swing.

    Blocks only when ``price`` is **below** the previous 1D peak (outside the
    current regime-high impulse) and the gap to that peak is under
    RISK_ATH_ENTRY_MIN_GAP_PCT. Price at/above that swing (new pump / ★ zone)
    is allowed. The regime / listing ATH is not used for this gate.
    """
    if not enabled(args):
        return False, ""
    min_gap = ath_entry_min_gap_pct(args)
    if min_gap <= 0:
        return False, ""
    if price <= 0:
        return False, ""
    del ath  # regime high is display/SL context only — not an entry block
    prior = prior_ath if prior_ath and prior_ath > 0 else None
    if prior is None:
        _regime, cached_prior = cached_ath_levels(symbol, last=price, args=args)
        prior = cached_prior
    if not prior or prior <= 0:
        return False, ""  # no prior swing → don't block ★
    if price >= float(prior):
        return False, ""  # above prior swing = current impulse / new high
    gap_p = distance_to_ath_pct(price, float(prior))
    if gap_p < min_gap:
        return True, (
            f"prior swing {prior:g} · {gap_p:.1f}% off < min {min_gap:g}% "
            f"(last {price:g})"
        )
    return False, ""


def _ensure_stop(
    symbol: str,
    is_long: bool,
    qty: float,
    trigger: float,
    tag: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
    *,
    dry: bool,
) -> dict[str, Any] | None:
    """Place/resync STOP_MARKET with tag RF. Returns algo dict or None."""
    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    tick = filt["tick_size"]
    step = filt["step_size"]
    price_dp = grid._dec_places(tick)
    qty_dp = grid._dec_places(step)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return None

    existing = staged.find_our_algo(symbol, tag, api, sec, recv)
    # SHORT stop sits above price — round UP so we don't sit inside the last print
    trig_d = grid._round_to(trigger, tick, ROUND_DOWN if is_long else ROUND_UP)
    trig_f = float(trig_d)
    trig_str = f"{trig_d:.{price_dp}f}"
    qty_str = f"{qty_d:.{qty_dp}f}"

    if existing:
        try:
            same_qty = abs(float(existing.get("quantity", 0) or 0) - float(qty_str)) < float(step) / 2
            same_trig = abs(float(existing.get("triggerPrice", 0) or 0) - trig_f) < float(tick) / 2
        except (TypeError, ValueError):
            same_qty = same_trig = False
        if same_qty and same_trig:
            return existing
        staged.cancel_our_algos(symbol, tag, api, sec, recv)

    if dry:
        print(
            f"{grid.DIM}[dry-run] ATH SL {tag} qty={qty_str} @ {trig_str}{grid.RESET}"
        )
        return {"dry": True, "triggerPrice": trig_str, "quantity": qty_str}

    # Avoid immediate trigger
    try:
        depth = grid.fetch_depth(symbol, 5)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        mark = (float(bids[0][0]) + float(asks[0][0])) / 2.0 if bids and asks else 0.0
    except Exception:
        mark = 0.0
    if mark > 0 and staged._stop_would_immediately_trigger(is_long, trig_f, mark, tick):
        print(
            f"{grid.YELLOW}ATH SL @ {grid.price_fmt(trig_f)} would trigger now "
            f"(mark {grid.price_fmt(mark)}) — skip place{grid.RESET}"
        )
        return None

    try:
        return staged.place_algo_order(
            symbol, is_long, "STOP_MARKET", qty_str, trig_str,
            hedge, api, sec, recv, client_tag=tag,
        )
    except Exception as exc:
        print(f"{grid.RED}ATH SL place failed: {exc}{grid.RESET}")
        return None


def sync_flat(
    symbol: str,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    staged.cancel_our_algos(symbol, TAG_PARTIAL, api, sec, recv)
    staged.cancel_our_algos(symbol, TAG_FULL, api, sec, recv)
    st = staged.load_state(symbol.upper())
    for k in (
        "risk_reduce_armed",
        "risk_reduce_filled",
        "risk_impulse_high",
        "risk_ath",
        "risk_partial_trig",
        "risk_full_trig",
        "risk_full_swing_high",
        "risk_full_source",
        "risk_armed_qty",
        "risk_block_rearm",
        "risk_tg_suggested",
        "risk_partial_loss_usdt",
        "risk_recovery_pct",
        "risk_recovery_active",
        "risk_reduce_pct",
        "risk_ath_sl_pct",
        "recovery_realized_usdt",
        "recovery_qty",
    ):
        st.pop(k, None)
    staged.save_state(symbol.upper(), st)


def run_once(
    symbol: str,
    side_is_long: bool,
    qty: float,
    entry: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    filt: dict[str, Decimal],
) -> None:
    if not enabled(args):
        return
    # ATH invalidation only applies to SHORT pump→stall books.
    if side_is_long:
        return
    if entry <= 0 or qty <= 0:
        return

    import orderbook_dca_grid as grid
    import orderbook_staged_exit as staged

    recv = int(getattr(args, "recv_window", 15000) or 15000)
    dry = bool(getattr(args, "dry_run", False))
    sym = symbol.upper()
    step = filt["step_size"]
    state = staged.load_state(sym)

    # Drop legacy partial RR if any leftover from older builds.
    staged.cancel_our_algos(sym, TAG_PARTIAL, api, sec, recv)

    ath = float(state.get("risk_ath", 0) or state.get("risk_impulse_high", 0) or 0)
    fetched = fetch_historical_ath(sym)
    if fetched and fetched > 0:
        if ath <= 0 or fetched > ath * 1.0001:
            if ath > 0 and fetched > ath * 1.0001:
                print(
                    f"{grid.CYAN}ATH SL: upgrade ATH {grid.price_fmt(ath)} → "
                    f"{grid.price_fmt(fetched)}{grid.RESET}"
                )
            ath = float(fetched)
            state["risk_ath"] = ath
            state["risk_impulse_high"] = ath  # back-compat for TG / status
    if ath <= 0:
        print(f"{grid.DIM}ATH SL: no historical ATH for {sym}{grid.RESET}")
        return

    sl_pct = ath_sl_pct(args)
    full_trig = ath_sl_trigger(ath, args)
    qty_d = grid._round_to(qty, step, ROUND_DOWN)
    if qty_d <= 0:
        return

    first_arm = not bool(state.get("risk_reduce_armed"))
    prev_trig = float(state.get("risk_full_trig", 0) or 0)
    resp = _ensure_stop(
        sym, side_is_long, float(qty_d), full_trig, TAG_FULL,
        args, hedge, api, sec, filt, dry=dry,
    )

    state.update({
        "risk_reduce_armed": True,
        "risk_reduce_filled": False,
        "risk_ath": ath,
        "risk_impulse_high": ath,
        "risk_full_trig": full_trig,
        "risk_full_source": "ath",
        "risk_armed_qty": float(qty_d),
        "risk_ath_sl_pct": sl_pct,
    })
    # Clear legacy RR fields
    for k in (
        "risk_partial_trig",
        "risk_reduce_pct",
        "risk_block_rearm",
        "risk_recovery_pct",
        "risk_recovery_active",
        "risk_partial_loss_usdt",
        "risk_full_swing_high",
    ):
        state.pop(k, None)
    staged.save_state(sym, state)

    trig_moved = prev_trig > 0 and abs(full_trig - prev_trig) / prev_trig > 0.002
    if resp is not None and (first_arm or trig_moved or not bool(state.get("risk_tg_suggested"))):
        if trig_moved:
            state.pop("risk_tg_suggested", None)
            staged.save_state(sym, state)
        print(
            f"{grid.BOLD}{grid.CYAN}✓ ATH SL · SHORT qty={float(qty_d):g} @ "
            f"{grid.price_fmt(full_trig)} "
            f"(ATH {grid.price_fmt(ath)} +{sl_pct:g}%){grid.RESET}"
        )
        _telegram_suggest(
            sym, "SHORT", float(qty_d), entry, ath, full_trig, sl_pct, args,
            hedge, api, sec, recv,
        )
    else:
        print(
            f"{grid.DIM}ATH SL sync · SHORT qty={float(qty_d):g} @ "
            f"{grid.price_fmt(full_trig)} · ATH {grid.price_fmt(ath)} "
            f"+{sl_pct:g}%{grid.RESET}"
        )


def _telegram_suggest(
    symbol: str,
    side: str,
    qty: float,
    entry: float,
    ath: float,
    full_trig: float,
    sl_pct: float,
    args: argparse.Namespace,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
) -> None:
    """ATH SL is announced on the IDEAL card — no separate public arm alert."""
    import orderbook_staged_exit as staged

    st = staged.load_state(symbol)
    if bool(st.get("risk_tg_suggested")):
        return
    st["risk_tg_suggested"] = True
    staged.save_state(symbol, st)
