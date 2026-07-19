#!/usr/bin/env python3
"""Live order-book wall dashboard for Binance Futures (stdlib only).

Polls depth, keeps a mid-price trail, and serves a local chart with top
bid/ask walls + imbalance. No API keys. Observation only (no trading).

Usage:
    python3 ob_live_chart.py BTCUSDT
    python3 ob_live_chart.py ETHUSDT --port 8765 --limit 100 --walls 8
    # open http://127.0.0.1:8765/
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

FAPI_BASE = os.getenv("FAPI_BASE", "https://fapi.binance.com").rstrip()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OB Live — __SYMBOL__</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root {
      --bg: #0e1116;
      --panel: #161b22;
      --text: #e6edf3;
      --muted: #8b949e;
      --bid: #3fb950;
      --ask: #f85149;
      --line: #30363d;
      --accent: #58a6ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Mono", "SF Mono", ui-monospace, Menlo, monospace;
      background: radial-gradient(1200px 600px at 10% -10%, #1a2332 0%, var(--bg) 55%);
      color: var(--text);
      min-height: 100vh;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 24px;
      align-items: baseline;
      padding: 16px 20px 8px;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 1.1rem;
      font-weight: 600;
      letter-spacing: 0.04em;
    }
    .meta { color: var(--muted); font-size: 0.8rem; }
    .pill {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 4px 10px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 4px;
      font-size: 0.8rem;
    }
    .bid { color: var(--bid); }
    .ask { color: var(--ask); }
    .wrap {
      display: grid;
      grid-template-columns: 1fr 280px;
      gap: 0;
      min-height: calc(100vh - 64px);
    }
    @media (max-width: 900px) {
      .wrap { grid-template-columns: 1fr; }
    }
    #chart {
      height: calc(100vh - 64px);
      min-height: 420px;
      border-right: 1px solid var(--line);
    }
    aside {
      padding: 12px 14px 20px;
      overflow: auto;
      background: rgba(22, 27, 34, 0.65);
    }
    h2 {
      margin: 14px 0 8px;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.72rem;
    }
    th, td {
      padding: 4px 2px;
      text-align: right;
      border-bottom: 1px solid rgba(48, 54, 61, 0.6);
    }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 500; }
    .bar-wrap {
      height: 8px;
      background: #21262d;
      border-radius: 2px;
      overflow: hidden;
      margin-top: 8px;
    }
    .bar {
      height: 100%;
      width: 50%;
      background: linear-gradient(90deg, var(--bid), #238636 50%, var(--ask));
      transition: width 0.25s ease;
    }
    .err { color: var(--ask); font-size: 0.8rem; padding: 8px 20px; }
  </style>
</head>
<body>
  <header>
    <h1>OB LIVE <span id="sym">__SYMBOL__</span></h1>
    <span class="pill"><span class="meta">mid</span> <strong id="mid">—</strong></span>
    <span class="pill"><span class="meta">spread</span> <span id="spread">—</span></span>
    <span class="pill"><span class="meta">imb</span> <span id="imb">—</span></span>
    <span class="meta" id="ts">waiting…</span>
  </header>
  <div id="err" class="err" hidden></div>
  <div class="wrap">
    <div id="chart"></div>
    <aside>
      <h2>Imbalance (band)</h2>
      <div class="bar-wrap"><div class="bar" id="imbBar"></div></div>
      <div class="meta" style="margin-top:6px" id="imbDetail">—</div>
      <h2 class="bid">Bid walls</h2>
      <table>
        <thead><tr><th>price</th><th>qty</th><th>USDT</th><th>%</th></tr></thead>
        <tbody id="bids"></tbody>
      </table>
      <h2 class="ask">Ask walls</h2>
      <table>
        <thead><tr><th>price</th><th>qty</th><th>USDT</th><th>%</th></tr></thead>
        <tbody id="asks"></tbody>
      </table>
    </aside>
  </div>
  <script>
    const SYMBOL = "__SYMBOL__";
    const chartEl = document.getElementById("chart");
    const chart = LightweightCharts.createChart(chartEl, {
      layout: {
        background: { color: "transparent" },
        textColor: "#8b949e",
        fontFamily: "IBM Plex Mono, SF Mono, monospace",
      },
      grid: {
        vertLines: { color: "rgba(48,54,61,0.45)" },
        horzLines: { color: "rgba(48,54,61,0.45)" },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    const series = chart.addAreaSeries({
      lineColor: "#58a6ff",
      topColor: "rgba(88,166,255,0.28)",
      bottomColor: "rgba(88,166,255,0.02)",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    const wallLines = [];
    function clearWalls() {
      while (wallLines.length) {
        series.removePriceLine(wallLines.pop());
      }
    }
    function fmt(n, d) {
      if (n == null || Number.isNaN(n)) return "—";
      return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
    }
    function wallRows(walls, tbody) {
      tbody.innerHTML = walls.map(w => `
        <tr>
          <td>${fmt(w.price, w.price >= 1000 ? 2 : 4)}</td>
          <td>${fmt(w.qty, 3)}</td>
          <td>${fmt(w.notional, 0)}</td>
          <td>${w.dist_pct.toFixed(3)}</td>
        </tr>`).join("");
    }
    function resize() {
      chart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });
    }
    window.addEventListener("resize", resize);
    resize();

    let lastLen = 0;
    async function tick() {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        document.getElementById("err").hidden = true;
        if (s.error) {
          document.getElementById("err").hidden = false;
          document.getElementById("err").textContent = s.error;
        }
        document.getElementById("mid").textContent = fmt(s.mid, s.mid >= 1000 ? 2 : 4);
        document.getElementById("spread").textContent = fmt(s.spread, s.mid >= 1000 ? 2 : 5);
        document.getElementById("imb").textContent = (s.imbalance * 100).toFixed(1) + "% bid";
        document.getElementById("imbBar").style.width = (s.imbalance * 100).toFixed(1) + "%";
        document.getElementById("imbDetail").textContent =
          `bid vol ${fmt(s.bid_vol, 2)} · ask vol ${fmt(s.ask_vol, 2)}`;
        document.getElementById("ts").textContent = s.ts_iso || "";
        wallRows(s.bid_walls || [], document.getElementById("bids"));
        wallRows(s.ask_walls || [], document.getElementById("asks"));

        const trail = (s.trail || []).map(p => ({ time: p.t, value: p.mid }));
        if (trail.length) {
          if (trail.length < lastLen) series.setData(trail);
          else if (trail.length === lastLen) series.update(trail[trail.length - 1]);
          else if (lastLen === 0) series.setData(trail);
          else {
            for (let i = lastLen; i < trail.length; i++) series.update(trail[i]);
          }
          lastLen = trail.length;
        }
        clearWalls();
        const maxN = Math.max(
          1,
          ...(s.bid_walls || []).map(w => w.notional),
          ...(s.ask_walls || []).map(w => w.notional),
        );
        for (const w of (s.bid_walls || []).slice(0, 5)) {
          wallLines.push(series.createPriceLine({
            price: w.price,
            color: `rgba(63,185,80,${0.35 + 0.55 * (w.notional / maxN)})`,
            lineWidth: w.notional / maxN > 0.6 ? 2 : 1,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: "B " + Math.round(w.notional / 1000) + "k",
          }));
        }
        for (const w of (s.ask_walls || []).slice(0, 5)) {
          wallLines.push(series.createPriceLine({
            price: w.price,
            color: `rgba(248,81,73,${0.35 + 0.55 * (w.notional / maxN)})`,
            lineWidth: w.notional / maxN > 0.6 ? 2 : 1,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: "A " + Math.round(w.notional / 1000) + "k",
          }));
        }
      } catch (e) {
        document.getElementById("err").hidden = false;
        document.getElementById("err").textContent = String(e);
      }
    }
    tick();
    setInterval(tick, __POLL_MS__);
  </script>
</body>
</html>
"""


class BookState:
    def __init__(
        self,
        symbol: str,
        *,
        limit: int,
        walls: int,
        band_pct: float,
        trail_sec: int,
        sample_sec: float,
    ) -> None:
        self.symbol = symbol.upper()
        self.limit = limit
        self.walls = walls
        self.band_pct = band_pct
        self.trail_sec = trail_sec
        self.sample_sec = sample_sec
        self._lock = threading.Lock()
        self._trail: deque[dict[str, float]] = deque()
        self._snapshot: dict[str, Any] = {
            "symbol": self.symbol,
            "mid": None,
            "spread": None,
            "imbalance": 0.5,
            "bid_vol": 0.0,
            "ask_vol": 0.0,
            "bid_walls": [],
            "ask_walls": [],
            "trail": [],
            "ts": 0.0,
            "ts_iso": "",
            "error": None,
        }
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    def _top_walls(
        self, levels: list[list[float]], mid: float, n: int
    ) -> list[dict[str, float]]:
        ranked = sorted(levels, key=lambda x: x[1], reverse=True)[: max(n * 3, n)]
        out: list[dict[str, float]] = []
        for price, qty in ranked:
            out.append(
                {
                    "price": price,
                    "qty": qty,
                    "notional": price * qty,
                    "dist_pct": (price - mid) / mid * 100 if mid else 0.0,
                }
            )
        out.sort(key=lambda w: abs(w["dist_pct"]))
        return out[:n]

    def _fetch(self) -> dict[str, Any]:
        url = f"{FAPI_BASE}/fapi/v1/depth?symbol={self.symbol}&limit={self.limit}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        bids = [[float(p), float(q)] for p, q in raw.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in raw.get("asks", [])]
        if not bids or not asks:
            raise RuntimeError("empty book")
        mid = (bids[0][0] + asks[0][0]) / 2
        spread = asks[0][0] - bids[0][0]
        lo = mid * (1 - self.band_pct / 100)
        hi = mid * (1 + self.band_pct / 100)
        bid_vol = sum(q for p, q in bids if p >= lo)
        ask_vol = sum(q for p, q in asks if p <= hi)
        total = bid_vol + ask_vol
        imb = (bid_vol / total) if total else 0.5
        now = time.time()
        return {
            "mid": mid,
            "spread": spread,
            "imbalance": imb,
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "bid_walls": self._top_walls(bids, mid, self.walls),
            "ask_walls": self._top_walls(asks, mid, self.walls),
            "ts": now,
            "ts_iso": time.strftime("%H:%M:%S", time.localtime(now)),
            "error": None,
        }

    def loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                snap = self._fetch()
                with self._lock:
                    t_sec = int(snap["ts"])
                    if self._trail and self._trail[-1]["t"] == t_sec:
                        self._trail[-1] = {"t": t_sec, "mid": snap["mid"]}
                    else:
                        self._trail.append({"t": t_sec, "mid": snap["mid"]})
                    cutoff = snap["ts"] - self.trail_sec
                    while self._trail and self._trail[0]["t"] < cutoff:
                        self._trail.popleft()
                    self._snapshot = {
                        "symbol": self.symbol,
                        **snap,
                        "trail": list(self._trail),
                    }
            except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError) as exc:
                with self._lock:
                    self._snapshot["error"] = str(exc)
                    self._snapshot["ts_iso"] = time.strftime("%H:%M:%S")
            elapsed = time.time() - t0
            self._stop.wait(max(0.05, self.sample_sec - elapsed))


def make_handler(state: BookState, ui_poll_ms: int):
    html = (
        DASHBOARD_HTML.replace("__SYMBOL__", state.symbol).replace(
            "__POLL_MS__", str(ui_poll_ms)
        )
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = html
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/state":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description="Live OB wall chart (Binance Futures)")
    p.add_argument("symbol", nargs="?", default="BTCUSDT")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--limit", type=int, default=100, help="depth levels")
    p.add_argument("--walls", type=int, default=8, help="top walls per side")
    p.add_argument("--band", type=float, default=0.15, help="imbalance band %% around mid")
    p.add_argument("--sample-sec", type=float, default=0.5, help="depth poll interval")
    p.add_argument("--trail-sec", type=int, default=900, help="price trail length (sec)")
    p.add_argument("--ui-ms", type=int, default=400, help="browser poll interval")
    args = p.parse_args()

    state = BookState(
        args.symbol,
        limit=args.limit,
        walls=args.walls,
        band_pct=args.band,
        trail_sec=args.trail_sec,
        sample_sec=args.sample_sec,
    )
    worker = threading.Thread(target=state.loop, name="depth", daemon=True)
    worker.start()

    handler = make_handler(state, args.ui_ms)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"OB live chart  {state.symbol}  →  {url}")
    print(f"depth limit={args.limit}  walls={args.walls}  sample={args.sample_sec}s  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        state.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
