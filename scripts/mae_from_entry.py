#!/usr/bin/env python3
"""Max adverse excursion for SHORT roundtrips from FIRST entry fill (ignore DCA avg).

Uses Binance userTrades + 1m klines. Adverse %% = (max_high − entry) / entry × 100.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> None:
    env = _load_env()
    api = env.get("BINANCE_API_KEY") or env.get("API_KEY") or ""
    sec = (
        env.get("BINANCE_SECRET_KEY")
        or env.get("BINANCE_API_SECRET")
        or env.get("API_SECRET")
        or ""
    )
    base = (env.get("FAPI_BASE") or "https://fapi.binance.com").rstrip("/")
    if not api or not sec:
        raise SystemExit("Missing Binance API keys in .env")

    def signed(path: str, params: dict) -> list | dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 15000
        q = urllib.parse.urlencode(params)
        sig = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{base}{path}?{q}&signature={sig}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def public(path: str, params: dict) -> list | dict:
        q = urllib.parse.urlencode(params)
        with urllib.request.urlopen(f"{base}{path}?{q}", timeout=30) as r:
            return json.loads(r.read().decode())

    trade_log = ROOT / ".state" / "pumpstall_trades.jsonl"
    rows = [json.loads(x) for x in trade_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    symbols = sorted({r["symbol"].upper() for r in rows})
    first_ts = min(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp() for r in rows)
    start_ms = int(first_ts * 1000) - 3_600_000
    now_ms = int(time.time() * 1000)
    print(f"closes={len(rows)} symbols={len(symbols)} from_ms={start_ms}")

    all_fills: list[dict] = []
    for sym in symbols:
        cursor = start_ms
        while cursor < now_ms:
            try:
                chunk = signed(
                    "/fapi/v1/userTrades",
                    {
                        "symbol": sym,
                        "startTime": cursor,
                        "endTime": now_ms,
                        "limit": 1000,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"ERR fills {sym}: {exc}")
                break
            if not isinstance(chunk, list) or not chunk:
                break
            all_fills.extend(chunk)
            last = int(chunk[-1]["time"])
            if last <= cursor:
                break
            cursor = last + 1
            if len(chunk) < 1000:
                break
            time.sleep(0.05)
        time.sleep(0.05)

    print(f"fills={len(all_fills)}")
    by_sym: dict[str, list] = defaultdict(list)
    for f in all_fills:
        by_sym[str(f["symbol"]).upper()].append(f)

    roundtrips: list[dict] = []
    for sym, fl in by_sym.items():
        fl = sorted(fl, key=lambda x: int(x["time"]))
        pos = 0.0
        first_entry = None
        entry_t = None
        for f in fl:
            qty = float(f["qty"])
            px = float(f["price"])
            t = int(f["time"])
            side = str(f["side"]).upper()
            signed_qty = qty if side == "BUY" else -qty
            prev = pos
            if abs(prev) < 1e-12 and signed_qty < 0:
                first_entry = px
                entry_t = t
            pos += signed_qty
            if abs(prev) > 1e-12 and abs(pos) < 1e-8:
                if first_entry and entry_t and prev < 0:
                    roundtrips.append(
                        {
                            "symbol": sym,
                            "entry": first_entry,
                            "entry_t": entry_t,
                            "exit_t": t,
                        }
                    )
                first_entry = None
                entry_t = None
                pos = 0.0

    print(f"short_roundtrips={len(roundtrips)}")

    hit = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    maes: list[float] = []
    details: list[tuple] = []

    for rt in roundtrips:
        start = int(rt["entry_t"])
        end = int(rt["exit_t"])
        max_high = float(rt["entry"])
        cur = start
        ok = True
        while cur < end:
            try:
                kl = public(
                    "/fapi/v1/klines",
                    {
                        "symbol": rt["symbol"],
                        "interval": "1m",
                        "startTime": cur,
                        "endTime": end,
                        "limit": 1500,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"ERR klines {rt['symbol']}: {exc}")
                ok = False
                break
            if not isinstance(kl, list) or not kl:
                break
            for k in kl:
                if int(k[0]) < start:
                    continue
                if int(k[0]) > end:
                    break
                h = float(k[2])
                if h > max_high:
                    max_high = h
            last_open = int(kl[-1][0])
            if last_open <= cur:
                break
            cur = last_open + 60_000
            if len(kl) < 1500:
                break
            time.sleep(0.02)
        if not ok:
            continue
        mae = (max_high - float(rt["entry"])) / float(rt["entry"]) * 100.0
        maes.append(mae)
        for thr in hit:
            if mae >= thr:
                hit[thr] += 1
        details.append((mae, rt["symbol"], rt["entry"], max_high))

    details.sort(reverse=True)
    print(f"analyzed={len(maes)}")
    for thr, n in hit.items():
        print(f"MAE>={thr}%: {n}")
    if maes:
        s = sorted(maes)
        print(
            f"max={max(maes):.3f}% median={s[len(s)//2]:.3f}% "
            f"mean={sum(maes)/len(maes):.3f}%"
        )
    print("top15 adverse from FIRST entry (not avg after DCA):")
    for mae, sym, entry, hi in details[:15]:
        print(f"  {mae:+.3f}% {sym} entry={entry:g} high={hi:g}")


if __name__ == "__main__":
    main()
