#!/usr/bin/env python3
"""Live order-book wall dashboard + OB scalper (Binance Futures).

Entries = near a bid/ask WALL + imbalance (order-book scalp), not BB-band
mean-reversion. Bollinger / EMA are optional regime filters.

Polls depth, draws mid + walls / depth profile, and trades with dynamic TP/SL.
By default places REAL market orders. Pass --dry-run for paper-only.

Usage:
    python3 ob_live_chart.py SOLUSDT                  # LIVE orders
    python3 ob_live_chart.py SOLUSDT --dry-run        # paper only
    python3 ob_live_chart.py BTCUSDT --notional 50
    # open http://127.0.0.1:8765/  (auto-picks next free port if busy)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from decimal import ROUND_DOWN, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

FAPI_BASE = os.getenv("FAPI_BASE", "https://fapi.binance.com").rstrip("/")


def bind_dashboard(
    host: str,
    start_port: int,
    handler: type[BaseHTTPRequestHandler],
    *,
    max_tries: int = 50,
) -> tuple[ThreadingHTTPServer, int]:
    """Bind HTTP server; if start_port is busy, try the next ports."""
    last_err: OSError | None = None
    for port in range(start_port, start_port + max_tries):
        try:
            return ThreadingHTTPServer((host, port), handler), port
        except OSError as exc:
            last_err = exc
            continue
    raise RuntimeError(
        f"no free port on {host} in {start_port}..{start_port + max_tries - 1}"
    ) from last_err


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
      --entry: #d2a8ff;
      --tp: #3fb950;
      --sl: #f85149;
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
      gap: 10px 18px;
      align-items: baseline;
      padding: 14px 20px 8px;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: 0.04em;
    }
    .meta { color: var(--muted); font-size: 0.78rem; }
    .pill {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 4px 10px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 4px;
      font-size: 0.78rem;
    }
    .pill.on { border-color: #3fb95066; }
    .pill.off { border-color: #f8514966; opacity: 0.85; }
    .bid { color: var(--bid); }
    .ask { color: var(--ask); }
    .entry { color: var(--entry); }
    .wrap {
      display: grid;
      grid-template-columns: 1fr 168px 380px;
      gap: 0;
      min-height: calc(100vh - 70px);
    }
    @media (max-width: 1280px) {
      .wrap { grid-template-columns: 1fr 150px 320px; }
    }
    @media (max-width: 1100px) {
      .wrap { grid-template-columns: 1fr 150px; }
      aside { display: none; }
    }
    @media (max-width: 720px) {
      .wrap { grid-template-columns: 1fr; }
      #profile { max-height: 220px; border-right: none; border-top: 1px solid var(--line); }
    }
    .chart-col {
      display: flex;
      flex-direction: column;
      min-width: 0;
      height: calc(100vh - 70px);
      border-right: 1px solid var(--line);
    }
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 6px 8px;
      border-bottom: 1px solid var(--line);
      background: rgba(13, 17, 23, 0.95);
      flex: 0 0 auto;
      align-items: stretch;
    }
    .filter-btn {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 88px;
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 4px;
      background: #161b22;
      color: var(--text);
      font: inherit;
      font-size: 0.68rem;
      cursor: pointer;
      text-align: left;
      line-height: 1.25;
    }
    .filter-btn:hover { border-color: #58a6ff88; }
    .filter-btn .fk { color: var(--muted); letter-spacing: 0.02em; font-size: 0.62rem; }
    .filter-btn .fv { font-weight: 600; }
    .filter-btn .fs { color: var(--muted); font-size: 0.6rem; }
    .filter-btn.on.ok { border-color: #3fb95088; background: #12261a; }
    .filter-btn.on.ok .fv { color: var(--bid); }
    .filter-btn.on.block { border-color: #f8514988; background: #2a1214; }
    .filter-btn.on.block .fv { color: var(--ask); }
    .filter-btn.on.wait { border-color: #d2992288; background: #241c0c; }
    .filter-btn.on.wait .fv { color: #e3b341; }
    .filter-btn.off { opacity: 0.42; border-style: dashed; }
    .filter-btn.locked { cursor: default; }
    .filter-btn.locked:hover { border-color: var(--line); }
    #chart {
      flex: 1 1 auto;
      height: 0;
      min-height: 320px;
    }
    #profile {
      height: calc(100vh - 70px);
      min-height: 420px;
      border-right: 1px solid var(--line);
      background: linear-gradient(180deg, #12161c 0%, #0e1116 100%);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #profile .ph {
      padding: 8px 8px 2px;
      font-size: 0.65rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      flex: 0 0 auto;
    }
    #profile .ph-book {
      padding: 0 8px 6px;
      font-size: 0.62rem;
      letter-spacing: 0.02em;
      text-transform: none;
      color: var(--muted);
      flex: 0 0 auto;
      line-height: 1.3;
      min-height: 1.1em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #profileLadder {
      flex: 1 1 auto;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      justify-content: center;
      padding: 4px 0;
      gap: 1px;
    }
    .pv-row {
      display: grid;
      grid-template-columns: 54px 1fr;
      align-items: center;
      gap: 4px;
      height: 7px;
      padding: 0 6px;
      flex: 1 1 0;
      min-height: 4px;
      max-height: 14px;
    }
    .pv-row.mid {
      height: 12px;
      max-height: 16px;
      border-top: 1px solid rgba(88,166,255,0.35);
      border-bottom: 1px solid rgba(88,166,255,0.35);
    }
    .pv-px {
      font-size: 0.58rem;
      color: var(--muted);
      text-align: right;
      line-height: 1;
      white-space: nowrap;
    }
    .pv-row.ask .pv-px { color: #e3b341; }
    .pv-row.bid .pv-px { color: #58a6ff; }
    .pv-track {
      height: 70%;
      min-height: 3px;
      display: flex;
      justify-content: flex-end;
      background: rgba(48,54,61,0.35);
      border-radius: 1px;
      overflow: hidden;
    }
    .pv-fill {
      height: 100%;
      border-radius: 1px;
      transition: width 0.2s ease;
    }
    .pv-row.ask .pv-fill { background: linear-gradient(90deg, #8b6914, #e3b341); }
    .pv-row.bid .pv-fill { background: linear-gradient(90deg, #1f6feb, #58a6ff); }
    .pv-row.wall .pv-fill { box-shadow: 0 0 0 1px rgba(255,255,255,0.25) inset; }
    aside {
      padding: 10px 16px 20px;
      overflow: auto;
      background: rgba(22, 27, 34, 0.65);
      min-width: 0;
    }
    aside .pos-box {
      word-break: break-word;
      line-height: 1.45;
    }
    h2 {
      margin: 14px 0 8px;
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.7rem;
    }
    th, td {
      padding: 4px 2px;
      text-align: right;
      border-bottom: 1px solid rgba(48, 54, 61, 0.6);
    }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 500; }
    .bar-wrap {
      height: 10px;
      background: #21262d;
      border-radius: 2px;
      overflow: hidden;
      margin-top: 8px;
      display: flex;
      width: 100%;
    }
    .bar-bid {
      height: 100%;
      width: 50%;
      background: var(--bid);
      transition: width 0.25s ease;
    }
    .bar-ask {
      height: 100%;
      width: 50%;
      background: var(--ask);
      transition: width 0.25s ease;
    }
    .err { color: var(--ask); font-size: 0.8rem; padding: 8px 20px; }
    .pos-box {
      border: 1px solid var(--line);
      background: #0d1117;
      padding: 8px 10px;
      border-radius: 4px;
      font-size: 0.75rem;
      line-height: 1.45;
    }
    .pos-box.empty { color: var(--muted); }
    .pos-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .pos-head h2 { margin: 14px 0 8px; }
    .btn-close {
      margin-top: 6px;
      padding: 4px 10px;
      border: 1px solid #f8514988;
      border-radius: 4px;
      background: #2a1214;
      color: var(--ask);
      font: inherit;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      cursor: pointer;
    }
    .btn-close:hover { border-color: #f85149; background: #3d1518; }
    .btn-close:disabled { opacity: 0.45; cursor: wait; }
    .btn-close[hidden] { display: none; }
    .pos-actions { display: flex; gap: 6px; align-items: center; margin-top: 6px; }
    .pos-actions .btn-close { margin-top: 0; }
    .btn-live {
      padding: 4px 10px;
      border: 1px solid var(--line);
      border-radius: 4px;
      background: #161b22;
      color: var(--muted);
      font: inherit;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      cursor: pointer;
    }
    .btn-live:hover { border-color: #58a6ff88; }
    .btn-live.on {
      border-color: #f8514988;
      background: #2a1214;
      color: var(--ask);
    }
    .btn-live.on:hover { border-color: #f85149; background: #3d1518; }
    .btn-live:disabled { opacity: 0.45; cursor: wait; }
    .btn-sl {
      padding: 4px 10px;
      border: 1px solid #f8514988;
      border-radius: 4px;
      background: #2a1214;
      color: var(--ask);
      font: inherit;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      cursor: pointer;
    }
    .btn-sl:hover { border-color: #f85149; background: #3d1518; }
    .btn-sl.off {
      border-color: var(--line);
      background: #161b22;
      color: var(--muted);
    }
    .btn-sl.off:hover { border-color: #58a6ff88; }
    .btn-sl:disabled { opacity: 0.45; cursor: wait; }
    #trades td { font-size: 0.65rem; }
    .win { color: var(--bid); }
    .loss { color: var(--ask); }
  </style>
</head>
<body>
  <header>
    <h1>OB LIVE <span id="sym">__SYMBOL__</span></h1>
    <span class="pill"><span class="meta">mid</span> <strong id="mid">—</strong></span>
    <span class="pill"><span class="meta">spread</span> <span id="spread">—</span></span>
    <span class="pill"><span class="meta">imb</span> <span id="imb">—</span></span>
    <span class="pill" id="regimePill"><span class="meta">regime</span> <span id="regime">—</span></span>
    <span class="pill" id="trendPill"><span class="meta">trend</span> <span id="trend">—</span></span>
    <span class="pill" id="smcPill"><span class="meta">smc</span> <span id="smc">—</span></span>
    <span class="pill" id="bookPill"><span class="meta">book</span> <span id="book">—</span></span>
    <span class="pill" id="confPill"><span class="meta">conf</span> <span id="conf">—</span></span>
    <span class="pill" id="sigPill"><span class="meta">signal</span> <span id="signal">FLAT</span></span>
    <span class="pill" id="sessPill"><span class="meta">session</span> <span id="sessNet">—</span></span>
    <span class="pill" id="modePill"><span class="meta">mode</span> <span id="modeLabel">—</span></span>
    <span class="meta" id="ts">waiting…</span>
  </header>
  <div id="err" class="err" hidden></div>
  <div class="wrap">
    <div class="chart-col">
      <div class="filter-bar" id="filterBar"></div>
      <div id="chart"></div>
    </div>
    <div id="profile">
      <div class="ph">Depth · USDT</div>
      <div class="ph-book" id="bookPh">—</div>
      <div id="profileLadder"></div>
    </div>
    <aside>
      <h2>Session PnL (paper)</h2>
      <div class="pos-box empty" id="sessionBox">no closed trades yet</div>
      <div class="pos-head">
        <h2>Position</h2>
        <div class="pos-actions">
          <button type="button" class="btn-live" id="btnLive" title="Toggle LIVE orders">LIVE</button>
          <button type="button" class="btn-sl" id="btnSl" title="Toggle stop-loss">SL</button>
          <button type="button" class="btn-close" id="btnClose" hidden>Close</button>
        </div>
      </div>
      <div class="pos-box empty" id="paperPos">flat — waiting for wall bounce</div>
      <h2>Live position</h2>
      <div class="pos-box empty" id="livePos">—</div>
      <h2>Confidence</h2>
      <div class="pos-box empty" id="confBox">—</div>
      <h2>Imbalance (band)</h2>
      <div class="bar-wrap"><div class="bar-bid" id="imbBid"></div><div class="bar-ask" id="imbAsk"></div></div>
      <div class="meta" style="margin-top:6px" id="imbDetail">—</div>
      <h2>Recent trades <span class="meta" id="tradesCount"></span></h2>
      <table>
        <thead><tr><th>side</th><th>qty</th><th>exit</th><th>why</th><th>pnl%</th></tr></thead>
        <tbody id="trades"></tbody>
      </table>
    </aside>
  </div>
  <script>
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
    const series = chart.addLineSeries({
      color: "#58a6ff",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    const bbUpper = chart.addLineSeries({
      color: "rgba(210,168,255,0.55)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    });
    const bbMid = chart.addLineSeries({
      color: "rgba(210,168,255,0.35)", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      priceLineVisible: false, lastValueVisible: false,
    });
    const bbLower = chart.addLineSeries({
      color: "rgba(210,168,255,0.55)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    });
    let priceLines = [];
    let lastMarkersKey = "";
    function clearLines() {
      while (priceLines.length) series.removePriceLine(priceLines.pop());
    }
    function addLine(price, color, title, width, style) {
      priceLines.push(series.createPriceLine({
        price, color, title: title || "",
        lineWidth: width != null ? width : 1,
        lineStyle: style != null ? style : LightweightCharts.LineStyle.Solid,
        axisLabelVisible: !!title,
      }));
    }
    function fmt(n, d) {
      if (n == null || Number.isNaN(n)) return "—";
      return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
    }
    function fmtElapsed(sec) {
      sec = Math.max(0, Math.floor(Number(sec) || 0));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      if (h > 0) return h + "h " + String(m).padStart(2, "0") + "m";
      if (m > 0) return m + "m " + String(s).padStart(2, "0") + "s";
      return s + "s";
    }
    function renderProfile(profile, mid) {
      const el = document.getElementById("profileLadder");
      if (!profile || !profile.length) {
        el.innerHTML = "";
        return;
      }
      const maxN = Math.max(1, ...profile.map(p => p.notional || 0));
      const pd = mid >= 1000 ? 2 : (mid >= 1 ? 4 : 5);
      el.innerHTML = profile.map(p => {
        const pct = Math.max(2, Math.round((p.notional / maxN) * 100));
        const isMid = p.side === "mid";
        const cls = isMid ? "mid" : (p.side + (p.wall ? " wall" : ""));
        const px = isMid ? "MID" : fmt(p.price, pd);
        const w = isMid ? 8 : pct;
        const title = isMid ? ("mid " + fmt(mid, pd)) : (p.side + " " + fmt(p.notional, 0) + " USDT");
        return `<div class="pv-row ${cls}" title="${title}">
          <div class="pv-px">${px}</div>
          <div class="pv-track"><div class="pv-fill" style="width:${w}%;${isMid ? "background:#58a6ff" : ""}"></div></div>
        </div>`;
      }).join("");
    }
    function resize() {
      chart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });
    }
    window.addEventListener("resize", resize);
    resize();

    let lastLen = 0;
    let filterSig = "";
    async function toggleFilter(id, enabled) {
      try {
        const r = await fetch("/api/filter", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, enabled }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
      } catch (e) {
        console.warn("filter toggle failed", e);
      }
    }
    let closing = false;
    async function closePosition() {
      const btn = document.getElementById("btnClose");
      if (closing) return;
      closing = true;
      if (btn) btn.disabled = true;
      try {
        const r = await fetch("/api/close", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || ("HTTP " + r.status));
      } catch (e) {
        console.warn("close failed", e);
        alert("Close failed: " + (e.message || e));
      } finally {
        closing = false;
        if (btn) btn.disabled = false;
      }
    }
    const btnCloseEl = document.getElementById("btnClose");
    if (btnCloseEl) btnCloseEl.addEventListener("click", closePosition);
    let togglingLive = false;
    function syncLiveBtn(dryRun) {
      const btn = document.getElementById("btnLive");
      if (!btn || togglingLive) return;
      const live = !dryRun;
      btn.classList.toggle("on", live);
      btn.textContent = live ? "LIVE" : "PAPER";
      btn.title = live
        ? "LIVE on — adopts/manages exchange position; click for PAPER (no new live entries)"
        : "PAPER — click to go LIVE (adopts open exchange position if any)";
    }
    async function toggleLive() {
      const btn = document.getElementById("btnLive");
      if (togglingLive || !btn) return;
      const nextLive = !btn.classList.contains("on");
      togglingLive = true;
      btn.disabled = true;
      try {
        const r = await fetch("/api/live", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: nextLive }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || ("HTTP " + r.status));
        syncLiveBtn(!!data.dry_run);
        if (data.adopted && data.adopt) {
          const a = data.adopt;
          console.info("adopted", a);
        }
      } catch (e) {
        console.warn("live toggle failed", e);
        alert("LIVE toggle failed: " + (e.message || e));
      } finally {
        togglingLive = false;
        btn.disabled = false;
      }
    }
    const btnLiveEl = document.getElementById("btnLive");
    if (btnLiveEl) btnLiveEl.addEventListener("click", toggleLive);
    let togglingSl = false;
    function syncSlBtn(slOn, slHits, slToDry) {
      const btn = document.getElementById("btnSl");
      if (!btn || togglingSl) return;
      const on = !!slOn;
      const hits = slHits != null ? Number(slHits) : 0;
      const lim = slToDry != null ? Number(slToDry) : 3;
      btn.classList.toggle("off", !on);
      btn.textContent = on
        ? (lim > 0 ? `SL ${hits}/${lim}` : "SL")
        : "SL OFF";
      btn.title = on
        ? (lim > 0
          ? `Stop-loss ON · ${hits}/${lim} SL → DRY · click to disable`
          : "Stop-loss ON — click to disable hard SL exits")
        : "Stop-loss OFF — click to re-enable hard SL exits";
    }
    async function toggleSl() {
      const btn = document.getElementById("btnSl");
      if (togglingSl || !btn) return;
      const next = btn.classList.contains("off");
      togglingSl = true;
      btn.disabled = true;
      try {
        const r = await fetch("/api/sl", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.ok) throw new Error(data.error || ("HTTP " + r.status));
        syncSlBtn(!!data.sl_enabled, data.sl_hits, data.sl_to_dry);
      } catch (e) {
        console.warn("sl toggle failed", e);
        alert("SL toggle failed: " + (e.message || e));
      } finally {
        togglingSl = false;
        btn.disabled = false;
      }
    }
    const btnSlEl = document.getElementById("btnSl");
    if (btnSlEl) btnSlEl.addEventListener("click", toggleSl);
    const filterBarEl = document.getElementById("filterBar");
    if (filterBarEl) {
      filterBarEl.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button.filter-btn");
        if (!btn || btn.dataset.toggle !== "1") return;
        const id = btn.dataset.id;
        const next = btn.dataset.en !== "1";
        // optimistic UI
        btn.dataset.en = next ? "1" : "0";
        btn.classList.toggle("off", !next);
        toggleFilter(id, next);
      });
    }
    function renderFilters(filters) {
      const bar = document.getElementById("filterBar");
      if (!bar) return;
      const sig = JSON.stringify(filters || []);
      if (sig === filterSig) return;
      filterSig = sig;
      bar.innerHTML = (filters || []).map(f => {
        const en = !!f.enabled;
        const st = f.state || (en ? "wait" : "off");
        const cls = "filter-btn "
          + (f.toggle === false ? "locked " : "")
          + (en ? ("on " + st) : "off");
        const tip = (f.status || "") + (f.toggle === false ? " · (setup)" : " · click to toggle");
        const stLabel = !en ? "OFF" : (st === "ok" ? "pass" : (st === "block" ? "BLOCK" : (st === "wait" ? "wait" : st)));
        return `<button type="button" class="${cls}" data-id="${f.id}" data-en="${en ? 1 : 0}" data-toggle="${f.toggle === false ? 0 : 1}" title="${tip}">
          <span class="fk">${f.label || f.id}</span>
          <span class="fv">${f.value != null ? f.value : "—"}</span>
          <span class="fs">${stLabel} · ${f.status || ""}</span>
        </button>`;
      }).join("");
    }
    async function tick() {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        window.__lastState = s;
        renderFilters(s.filters || []);
        document.getElementById("err").hidden = true;
        if (s.error) {
          document.getElementById("err").hidden = false;
          document.getElementById("err").textContent = s.error;
        }
        const pd = s.mid >= 1000 ? 2 : 4;
        document.getElementById("mid").textContent = fmt(s.mid, pd);
        document.getElementById("spread").textContent = fmt(s.spread, s.mid >= 1000 ? 2 : 5);
        const bidPct = Math.max(0, Math.min(100, (s.imbalance || 0.5) * 100));
        const askPct = 100 - bidPct;
        document.getElementById("imb").textContent =
          bidPct.toFixed(1) + "% bid / " + askPct.toFixed(1) + "% ask";
        document.getElementById("imbBid").style.width = bidPct.toFixed(1) + "%";
        document.getElementById("imbAsk").style.width = askPct.toFixed(1) + "%";
        const bookMeta = s.book || {};
        let imbDetail =
          `band bid vol ${fmt(s.bid_vol, 2)} · ask vol ${fmt(s.ask_vol, 2)}`;
        if (bookMeta.pressure != null) {
          imbDetail +=
            `<br>ladder ${bookMeta.label || "—"} ` +
            `${(Number(bookMeta.pressure)*100).toFixed(0)}% bid` +
            ` · ${bookMeta.trend || ""}` +
            ` · ${fmt(bookMeta.bid_usdt, 0)}/${fmt(bookMeta.ask_usdt, 0)} USDT`;
        }
        const started = (s.session && s.session.started_at) || s.started_at;
        const elapsedSec = started
          ? Math.max(0, Math.floor((Date.now() / 1000) - Number(started)))
          : (s.elapsed_sec || 0);
        document.getElementById("ts").textContent = fmtElapsed(elapsedSec);
        document.getElementById("ts").title = s.ts_iso ? ("clock " + s.ts_iso) : "";

        const regime = s.regime || {};
        const regimeEl = document.getElementById("regime");
        const regimePill = document.getElementById("regimePill");
        regimeEl.textContent = regime.label || "—";
        regimePill.className = "pill " + (regime.tradeable ? "on" : "off");

        const trend = s.trend || {};
        const trendEl = document.getElementById("trend");
        const trendPill = document.getElementById("trendPill");
        trendEl.textContent = trend.label || "—";
        trendEl.className = trend.label === "bullish" ? "bid" : (trend.label === "bearish" ? "ask" : "");
        trendPill.className = "pill " + (trend.label === "bullish" || trend.label === "bearish" ? "on" : "");
        trendPill.title = trend.detail || "";

        const book = s.book || {};
        const bookEl = document.getElementById("book");
        const bookPill = document.getElementById("bookPill");
        const bookPh = document.getElementById("bookPh");
        const bp = book.pressure != null ? Number(book.pressure) * 100 : 50;
        const bookLbl = book.label || "—";
        bookEl.textContent = bookLbl;
        bookEl.className = bookLbl === "bid-heavy" ? "bid" : (bookLbl === "ask-heavy" ? "ask" : "");
        bookPill.className = "pill " + (bookLbl === "bid-heavy" || bookLbl === "ask-heavy" ? "on" : "");
        bookPill.title = (book.detail || "") + (book.trend ? (" · " + book.trend) : "");
        if (bookPh) {
          bookPh.textContent = bookLbl !== "—"
            ? (bookLbl + " " + bp.toFixed(0) + "% bid " + (book.trend || "")).trim()
            : "—";
          bookPh.className = "ph-book " + (
            bookLbl === "bid-heavy" ? "bid" : (bookLbl === "ask-heavy" ? "ask" : "meta")
          );
        }

        const conf = s.confidence || {};
        const confScore = conf.score != null ? Number(conf.score) : 0;
        const confMin = conf.min != null ? Number(conf.min) : 0;
        const confEl = document.getElementById("conf");
        const confPill = document.getElementById("confPill");
        confEl.textContent = confScore.toFixed(0) + "/" + confMin.toFixed(0);
        confEl.className = confScore >= confMin ? "win" : "loss";
        confPill.className = "pill " + (confScore >= confMin ? "on" : "off");
        const confBox = document.getElementById("confBox");
        const parts = conf.parts || {};
        confBox.className = "pos-box";
        confBox.innerHTML =
          `score <strong class="${confScore >= confMin ? "win" : "loss"}">${confScore.toFixed(0)}</strong>` +
          ` / min ${confMin.toFixed(0)} · side ${conf.side || "—"}<br>` +
          `<span class="meta">imb ${parts.imb||0} · wall ${parts.wall||0}` +
          ` · ratio ${parts.ratio||0} · book ${parts.book||0} · bb ${parts.bb||0}` +
          ` · ema ${parts.ema||0} · mom ${parts.mom||0} · tp ${parts.tp||0}</span>` +
          (conf.wall_ratio != null
            ? `<br><span class="meta">wall× ${Number(conf.wall_ratio).toFixed(2)} · mom ${Number(conf.mom_pct||0).toFixed(3)}%</span>`
            : "");

        const sig = (s.signal || "flat").toUpperCase();
        const sigEl = document.getElementById("signal");
        const sigPill = document.getElementById("sigPill");
        sigEl.textContent = sig;
        sigEl.className = sig === "LONG" ? "bid" : sig === "SHORT" ? "ask" : "";
        sigPill.className = "pill " + (sig === "FLAT" ? "" : "on");
        sigPill.title = s.block_reason || "";
        if (sig === "FLAT" && s.block_reason) {
          sigEl.textContent = "FLAT";
          imbDetail = `wait: ${s.block_reason} · ` + imbDetail;
        }
        document.getElementById("imbDetail").textContent = imbDetail;

        const sess = s.session || {};
        const sessNet = sess.net_pct != null ? sess.net_pct : 0;
        const sessEl = document.getElementById("sessNet");
        const sessPill = document.getElementById("sessPill");
        sessEl.textContent = (sessNet >= 0 ? "+" : "") + Number(sessNet).toFixed(3) + "%";
        sessEl.className = sessNet >= 0 ? "win" : "loss";
        sessPill.className = "pill " + (sessNet >= 0 ? "on" : "off");
        const modeEl = document.getElementById("modeLabel");
        const modePill = document.getElementById("modePill");
        if (s.dry_run) {
          modeEl.textContent = "DRY-RUN";
          modeEl.className = "";
          modePill.className = "pill";
        } else {
          modeEl.textContent = "LIVE";
          modeEl.className = "ask";
          modePill.className = "pill off";
        }
        syncLiveBtn(!!s.dry_run);
        syncSlBtn(s.sl_enabled !== false, s.sl_hits, s.sl_to_dry);
        const smc = s.structure || {};
        const smcEl = document.getElementById("smc");
        const smcPill = document.getElementById("smcPill");
        if (smcEl && smcPill) {
          const choch = smc.choch || "none";
          const bits = [];
          if (choch !== "none") bits.push("CHoCH " + choch);
          if (smc.eql) bits.push("EQL");
          if (smc.eqh) bits.push("EQH");
          if (!bits.length) {
            bits.push(smc.label || "flat");
          }
          smcEl.textContent = bits.join(" · ");
          smcEl.className = choch === "long" || smc.eql
            ? "bid"
            : (choch === "short" || smc.eqh ? "ask" : "");
          const biased = choch !== "none" || smc.eql || smc.eqh;
          smcPill.className = "pill " + (biased ? "on" : "");
          smcPill.title = smc.detail || smc.reason || "";
        }
        const sessBox = document.getElementById("sessionBox");
        const n = sess.trades || 0;
        const feeSrc = sess.fee_source === "binance"
          ? `Binance ${sess.fee_mode || "taker"} (m ${(Number(sess.maker_pct||0)).toFixed(4)}% · t ${(Number(sess.taker_pct||0)).toFixed(4)}%)`
          : `fallback (no API keys)`;
        const openN = (s.paper && s.paper.side) ? 1 : 0;
        const tradesCountEl = document.getElementById("tradesCount");
        if (tradesCountEl) {
          tradesCountEl.textContent = n ? `(${n})` : "";
        }
        if (n === 0 && !openN) {
          sessBox.className = "pos-box empty";
          sessBox.innerHTML =
            `trades <strong>0</strong><br><span class="meta">fees ${Number(sess.fee_rt_pct||0).toFixed(4)}%/RT · ${feeSrc}</span>`;
        } else {
          sessBox.className = "pos-box";
          const u = sess.unrealized_pct || 0;
          const uCls = u >= 0 ? "win" : "loss";
          sessBox.innerHTML =
            `trades <strong>${n}</strong> closed` +
            (openN ? ` · <span class="entry">1 open</span>` : "") +
            ` · W/L ${sess.wins || 0}/${sess.losses || 0}<br>` +
            `gross <span class="${(sess.gross_pct||0) >= 0 ? "win" : "loss"}">${(sess.gross_pct||0) >= 0 ? "+" : ""}${Number(sess.gross_pct||0).toFixed(3)}%</span>` +
            ` · fees −${Number(sess.fees_pct||0).toFixed(3)}%<br>` +
            `net <span class="${sessNet >= 0 ? "win" : "loss"}"><strong>${sessNet >= 0 ? "+" : ""}${Number(sessNet).toFixed(3)}%</strong></span>` +
            (sess.notional ? ` · ≈ ${((sess.net_pct||0) * sess.notional / 100).toFixed(2)} USDT` : "") +
            `<br>open uPnL <span class="${uCls}">${u >= 0 ? "+" : ""}${Number(u).toFixed(3)}%</span>` +
            ` · total <span class="${(sess.total_pct||0) >= 0 ? "win" : "loss"}">${(sess.total_pct||0) >= 0 ? "+" : ""}${Number(sess.total_pct||0).toFixed(3)}%</span>` +
            `<br><span class="meta">fees ${Number(sess.fee_rt_pct||0).toFixed(4)}%/RT · ${feeSrc}</span>`;
        }

        const paper = s.paper || {};
        const paperBox = document.getElementById("paperPos");
        const btnClose = document.getElementById("btnClose");
        if (btnClose && !closing) btnClose.hidden = !paper.side;
        if (paper.side) {
          const pnl = paper.pnl_pct;
          const pnlCls = pnl >= 0 ? "win" : "loss";
          paperBox.className = "pos-box";
          const peak = paper.peak_pnl_pct != null ? paper.peak_pnl_pct : pnl;
          const beOn = !!paper.be_locked;
          const dcaN = paper.dca_count != null ? Number(paper.dca_count) : 0;
          const beNeed = Number(paper.be_need_pct != null ? paper.be_need_pct : 0.04);
          const beLine = beOn
            ? `<span class="win">BE LOCKED</span> @ ${fmt(paper.entry, pd)}`
            : `<span class="meta">BE pending</span> (need ~+${Math.max(0, beNeed - pnl).toFixed(3)}% more)`;
          const src = paper.source === "live" ? "LIVE" : "paper";
          const qtyBit = paper.qty ? ` · qty ${fmt(paper.qty, 4)}` : "";
          const dcaBit = dcaN > 0
            ? ` · <span class="entry">DCA×${dcaN}</span>`
            : "";
          const slOn = s.sl_enabled !== false;
          const slBit = slOn
            ? `SL ${fmt(paper.sl, pd)}`
            : `<span class="meta">SL OFF</span>`;
          paperBox.innerHTML =
            `<span class="${paper.side === "long" ? "bid" : "ask"}">${paper.side.toUpperCase()}</span>` +
            ` avg <span class="entry">${fmt(paper.entry, pd)}</span>` +
            ` <span class="meta">[${src}]</span>${qtyBit}${dcaBit}<br>` +
            `TP ${fmt(paper.tp, pd)} · ${slBit}` +
            ` <span class="meta">(${paper.exits || "wall"})</span><br>` +
            `${beLine}<br>` +
            `wall ${fmt(paper.wall_price, pd)} · ` +
            `pnl <span class="${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(3)}%</span>` +
            ` · peak ${peak >= 0 ? "+" : ""}${Number(peak).toFixed(3)}%`;
        } else {
          paperBox.className = "pos-box empty";
          let flatMsg = s.block_reason
            ? ("flat — " + s.block_reason)
            : "flat — waiting for wall bounce";
          if (s.order_error) flatMsg += " · err: " + s.order_error;
          paperBox.textContent = flatMsg;
        }

        const live = s.live || {};
        const liveBox = document.getElementById("livePos");
        if (!s.live_enabled) {
          liveBox.className = "pos-box empty";
          liveBox.textContent = "off (pass --live-pos)";
        } else if (live.side) {
          liveBox.className = "pos-box";
          liveBox.innerHTML =
            `<span class="${live.side === "long" ? "bid" : "ask"}">${live.side.toUpperCase()}</span>` +
            ` @ <span class="entry">${fmt(live.entry, pd)}</span> · qty ${fmt(live.qty, 4)}` +
            `<br>uPnL ${fmt(live.upnl, 2)} USDT`;
        } else {
          liveBox.className = "pos-box empty";
          liveBox.textContent = live.error || "flat on exchange";
        }

        const trades = s.trades || [];
        document.getElementById("trades").innerHTML = trades.slice().reverse().slice(0, 12).map(t => {
          const net = t.net_pct != null ? t.net_pct : t.pnl_pct;
          const qty = t.qty != null && t.qty > 0 ? fmt(t.qty, 4) : "—";
          return `
          <tr>
            <td class="${t.side === "long" ? "bid" : "ask"}">${t.side}</td>
            <td>${qty}</td>
            <td>${fmt(t.exit, pd)}</td>
            <td>${t.why || "—"}</td>
            <td class="${net >= 0 ? "win" : "loss"}">${net >= 0 ? "+" : ""}${Number(net).toFixed(3)}</td>
          </tr>`;
        }).join("");

        renderProfile(s.depth_profile || [], s.mid);

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

        const bb = s.bb_trail || [];
        if (bb.length) {
          bbUpper.setData(bb.map(p => ({ time: p.t, value: p.upper })));
          bbMid.setData(bb.map(p => ({ time: p.t, value: p.mid })));
          bbLower.setData(bb.map(p => ({ time: p.t, value: p.lower })));
        }

        clearLines();
        const maxN = Math.max(
          1,
          ...(s.bid_walls || []).map(w => w.notional),
          ...(s.ask_walls || []).map(w => w.notional),
        );
        for (const w of (s.bid_walls || []).slice(0, 4)) {
          const a = 0.22 + 0.35 * (w.notional / maxN);
          // Lines only — no axis labels / size numbers
          addLine(w.price, `rgba(63,185,80,${a})`, "", 1, LightweightCharts.LineStyle.Dotted);
        }
        for (const w of (s.ask_walls || []).slice(0, 4)) {
          const a = 0.22 + 0.35 * (w.notional / maxN);
          addLine(w.price, `rgba(248,81,73,${a})`, "", 1, LightweightCharts.LineStyle.Dotted);
        }
        if (paper.side) {
          addLine(paper.entry, "#d2a8ff", "E", 1);
          // BE line only when actually locked
          if (paper.be_locked) {
            addLine(paper.entry, "#e3b341", "BE", 1, LightweightCharts.LineStyle.Solid);
          }
          addLine(paper.tp, "#3fb950", "TP", 1, LightweightCharts.LineStyle.Dashed);
          if (s.sl_enabled !== false) {
            const slLabel = paper.be_locked && Math.abs(paper.sl - paper.entry) / paper.entry < 1e-8
              ? "BE" : "SL";
            addLine(paper.sl, "#f85149", slLabel, 1, LightweightCharts.LineStyle.Dashed);
          }
        }
        if (live.side && live.entry) {
          addLine(live.entry, "#ffa657", "L", 1, LightweightCharts.LineStyle.SparseDotted);
        }

        const markers = (s.markers || []).map(m => ({
          time: m.t,
          position: m.side === "long" ? "belowBar" : "aboveBar",
          color: m.kind === "exit" ? (m.win ? "#3fb950" : "#f85149") : (m.side === "long" ? "#3fb950" : "#f85149"),
          shape: m.kind === "exit" ? "circle" : (m.side === "long" ? "arrowUp" : "arrowDown"),
          text: m.label || "",
        }));
        const key = JSON.stringify(markers);
        if (key !== lastMarkersKey) {
          series.setMarkers(markers);
          lastMarkersKey = key;
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


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_klines(symbol: str, interval: str, limit: int) -> list[list]:
    q = f"symbol={symbol.upper()}&interval={interval}&limit={limit}"
    return _http_get_json(f"{FAPI_BASE}/fapi/v1/klines?{q}")


def bollinger(closes: list[float], period: int, std_mult: float) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = statistics.fmean(window)
    var = statistics.pvariance(window)
    sd = math.sqrt(var)
    return mid + std_mult * sd, mid, mid - std_mult * sd


def fetch_commission_rates(
    symbol: str, api: str, sec: str, recv_window: int
) -> dict[str, float]:
    """User commission rates from Binance Futures (fractions → percent).

    GET /fapi/v1/commissionRate
    """
    from orderbook_dca_grid import _signed_request

    raw = _signed_request(
        "GET",
        "/fapi/v1/commissionRate",
        {"symbol": symbol.upper()},
        api,
        sec,
        recv_window,
    )
    maker = float(raw.get("makerCommissionRate", 0) or 0) * 100.0
    taker = float(raw.get("takerCommissionRate", 0) or 0) * 100.0
    return {"maker_pct": maker, "taker_pct": taker}


def round_trip_fee_pct(maker_pct: float, taker_pct: float, mode: str) -> float:
    mode = (mode or "taker").lower()
    if mode == "maker":
        return maker_pct * 2.0
    if mode == "mixed":
        return maker_pct + taker_pct
    return taker_pct * 2.0  # taker entry + taker exit (conservative for mid fills)


def _cid(prefix: str, symbol: str, side: str) -> str:
    ts = int(time.time()) % 1_000_000
    tag = "L" if side == "long" else "S"
    return f"{prefix}{tag}{symbol.upper()}{ts}"[:36]


def market_open(
    symbol: str,
    is_long: bool,
    qty_str: str,
    hedge: bool,
    api: str,
    sec: str,
    recv: int,
    *,
    cid: str,
) -> dict[str, Any]:
    from orderbook_dca_grid import _signed_request

    side = "BUY" if is_long else "SELL"
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side,
        "type": "MARKET",
        "quantity": qty_str,
        "newClientOrderId": cid,
    }
    if hedge:
        params["positionSide"] = "LONG" if is_long else "SHORT"
    return _signed_request("POST", "/fapi/v1/order", params, api, sec, recv)


def qty_exchange_min(price: float, filt: dict[str, Decimal]) -> tuple[str, float]:
    """Smallest valid qty: LOT_SIZE min_qty, bumped to MIN_NOTIONAL if needed."""
    from orderbook_dca_grid import _dec_places

    step = filt["step_size"]
    qty_dp = _dec_places(step)
    if price <= 0:
        raise ValueError("invalid price for sizing")
    qty_d = filt["min_qty"]
    while qty_d * Decimal(str(price)) < filt["min_notional"]:
        qty_d += step
    qty_str = f"{qty_d:.{qty_dp}f}"
    return qty_str, float(qty_d)


def qty_for_notional(notional: float, price: float, filt: dict[str, Decimal]) -> tuple[str, float]:
    from orderbook_dca_grid import _dec_places, _round_to

    step = filt["step_size"]
    qty_dp = _dec_places(step)
    if price <= 0:
        raise ValueError("invalid price for sizing")
    qty_d = _round_to(notional / price, step, ROUND_DOWN)
    if qty_d < filt["min_qty"]:
        qty_d = filt["min_qty"]
    while qty_d * Decimal(str(price)) < filt["min_notional"]:
        qty_d += step
    qty_str = f"{qty_d:.{qty_dp}f}"
    return qty_str, float(qty_d)


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
        tp_pct: float,
        sl_pct: float,
        min_sl_pct: float,
        touch_pct: float,
        min_wall_usdt: float,
        imb_long: float,
        imb_short: float,
        bb_period: int,
        bb_std: float,
        bb_interval: str,
        bb_pad_pct: float,
        cooldown_sec: float,
        paper: bool,
        dry_run: bool,
        live_pos: bool,
        api_key: str,
        api_secret: str,
        recv_window: int,
        hedge: bool,
        filt: dict[str, Decimal] | None,
        flip_exit: bool,
        rev_exit: bool,
        min_lock_pct: float,
        rev_pct: float,
        giveback_exit: bool,
        exits: str,
        sl_buffer_pct: float,
        max_tp_pct: float,
        fee_rt_pct: float,
        notional: float,
        min_edge_pct: float,
        min_hold_sec: float,
        net_exits: bool,
        fee_mode: str,
        protect_be: bool,
        protect_trail: bool,
        be_buffer_pct: float,
        sl_grace_sec: float,
        ema_filter: bool,
        ema_interval: str,
        ema_fast: int,
        ema_slow: int,
        ema_slope_min: float,
        min_confidence: float,
        min_wall_ratio: float,
        mom_max_against: float,
        require_bounce: bool,
        bb_strict: bool,
        book_filter: bool,
    ) -> None:
        self.symbol = symbol.upper()
        self.limit = limit
        self.walls = walls
        self.band_pct = band_pct
        self.trail_sec = trail_sec
        self.sample_sec = sample_sec
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.min_sl_pct = min_sl_pct
        self.touch_pct = touch_pct
        self.min_wall_usdt = min_wall_usdt
        self.imb_long = imb_long
        self.imb_short = imb_short
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.bb_interval = bb_interval
        self.bb_pad_pct = bb_pad_pct  # allow entries slightly outside BB (scalp re-entry)
        self.cooldown_sec = cooldown_sec
        self.paper_enabled = paper
        self.dry_run = dry_run
        self.live_enabled = live_pos or (not dry_run)
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.hedge = hedge
        self.filt = filt
        self._last_order_error: str | None = None
        self.flip_exit = flip_exit
        self.rev_exit = rev_exit
        self.min_lock_pct = min_lock_pct
        self.rev_pct = rev_pct
        self.giveback_exit = giveback_exit
        self.exits = exits  # "wall" | "pct"
        self.sl_buffer_pct = sl_buffer_pct
        self.max_tp_pct = max_tp_pct
        self.fee_rt_pct = fee_rt_pct  # fallback until Binance commissionRate loads
        self.fee_fallback_pct = fee_rt_pct
        self.fee_mode = fee_mode
        self.notional = notional
        self.min_edge_pct = min_edge_pct  # extra %% beyond fees for TP target
        self.min_hold_sec = min_hold_sec
        self.net_exits = net_exits  # skip soft exits unless gross covers fees
        self.protect_be = protect_be
        self.protect_trail = protect_trail
        self.be_buffer_pct = be_buffer_pct
        self.sl_grace_sec = sl_grace_sec
        self.ema_filter = ema_filter
        self.ema_interval = ema_interval
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_slope_min = ema_slope_min
        self.min_confidence = min_confidence
        self.min_wall_ratio = min_wall_ratio
        self.mom_max_against = mom_max_against
        self.require_bounce = require_bounce
        self.bb_strict = bb_strict
        self.book_filter = book_filter
        # Runtime toggles (UI); start matching CLI / sensible defaults
        self.bb_filter = True
        self.mom_filter = True
        self.conf_filter = True
        self.ratio_filter = min_wall_ratio > 0
        self.dca_enabled = True
        self.dca_max = 8
        self.dca_min_loss_pct = 0.05  # only DCA after this unrealized loss %%
        # Treat walls within this %% as the same OB (0.02%% was too tight → stacked DCAs)
        self.dca_space_pct = 0.10
        self.dca_cooldown_sec = 12.0
        # When fee-covered: snap TP to nearest opposite OB and close on touch
        self.ob_tp_exit = True
        # Hard SL exits (UI toggle); TP / soft exits stay active
        self.sl_enabled = True
        # After N session SL hits while LIVE → force PAPER (0 = never)
        self.sl_to_dry = 3
        self.sl_hits = 0
        # Oscillator / SMC filters (UI toggles)
        self.rsi_filter = False
        self.adx_filter = True
        self.smc_filter = False
        self.osc_interval = "5m"
        self.rsi_oversold = 30.0
        self.rsi_overbought = 70.0
        self.adx_period = 14
        self.adx_min = 20.0
        self.structure_interval = "5m"

        self._lock = threading.Lock()
        self._trail: deque[dict[str, float]] = deque()
        self._bb_trail: deque[dict[str, float]] = deque()
        self._book_hist: deque[float] = deque(maxlen=48)
        self._markers: list[dict[str, Any]] = []
        self._trades: list[dict[str, Any]] = []
        self._session = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "gross_pct": 0.0,
            "fees_pct": 0.0,
            "net_pct": 0.0,
            "fee_rt_pct": fee_rt_pct,
            "fee_source": "fallback",
            "fee_mode": fee_mode,
            "maker_pct": 0.0,
            "taker_pct": 0.0,
            "notional": notional,
            "dry_run": dry_run,
            "started_at": time.time(),
        }
        self._paper: dict[str, Any] | None = None
        self._live: dict[str, Any] = {}
        self._bb: dict[str, Any] = {"label": "n/a", "tradeable": True}
        self._trend: dict[str, Any] = {"label": "n/a", "detail": ""}
        self._book: dict[str, Any] = {
            "label": "n/a",
            "pressure": 0.5,
            "trend": "n/a",
            "detail": "",
        }
        self._osc: dict[str, Any] = {
            "rsi": None,
            "adx": None,
            "di_plus": None,
            "di_minus": None,
            "rsi_side": "none",
            "adx_side": "none",
            "detail": "",
        }
        self._structure: dict[str, Any] = {
            "label": "n/a",
            "choch": "none",
            "eqh": False,
            "eql": False,
            "allow_long": False,
            "allow_short": False,
            "detail": "",
        }
        self._confidence: dict[str, Any] = {
            "score": 0.0,
            "min": min_confidence,
            "side": None,
            "parts": {},
        }
        self._ema_snap: Any = None
        self._osc_snap: Any = None
        self._struct_snap: Any = None
        self._signal = "flat"
        self._block_reason = "starting…"
        self._filters: list[dict[str, Any]] = []
        self._cooldown_until = 0.0
        self._bb_next = 0.0
        self._ema_next = 0.0
        self._osc_next = 0.0
        self._struct_next = 0.0
        self._live_next = 0.0
        self._fee_next = 0.0
        self._closes: list[float] = []
        self._snapshot: dict[str, Any] = self._empty_snap()
        self._stop = threading.Event()

    def _empty_snap(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "mid": None,
            "spread": None,
            "imbalance": 0.5,
            "bid_vol": 0.0,
            "ask_vol": 0.0,
            "bid_walls": [],
            "ask_walls": [],
            "depth_profile": [],
            "trail": [],
            "bb_trail": [],
            "regime": self._bb,
            "signal": "flat",
            "block_reason": "starting…",
            "dry_run": self.dry_run,
            "sl_enabled": self.sl_enabled,
            "sl_hits": self.sl_hits,
            "sl_to_dry": self.sl_to_dry,
            "trend": dict(self._trend),
            "structure": dict(self._structure),
            "osc": dict(self._osc),
            "book": dict(self._book),
            "confidence": dict(self._confidence),
            "filters": [],
            "paper": {},
            "live": {},
            "live_enabled": self.live_enabled,
            "markers": [],
            "trades": [],
            "session": dict(self._session),
            "ts": 0.0,
            "ts_iso": "",
            "error": None,
        }

    def set_filter(self, fid: str, enabled: bool) -> dict[str, Any]:
        """Toggle a runtime filter from the UI. Returns updated filter flags."""
        key = {
            "bb": "bb_filter",
            "ema": "ema_filter",
            "book": "book_filter",
            "mom": "mom_filter",
            "bounce": "require_bounce",
            "ratio": "ratio_filter",
            "conf": "conf_filter",
            "bb_strict": "bb_strict",
            "dca": "dca_enabled",
            "obtp": "ob_tp_exit",
            "sl": "sl_enabled",
            "rsi": "rsi_filter",
            "adx": "adx_filter",
            "smc": "smc_filter",
        }.get(fid)
        if key is None:
            raise KeyError(f"unknown filter {fid}")
        setattr(self, key, bool(enabled))
        return self.filter_flags()

    def set_sl(self, enabled: bool) -> dict[str, Any]:
        """Toggle hard stop-loss exits from the UI (TP / soft exits unchanged)."""
        self.sl_enabled = bool(enabled)
        with self._lock:
            self._snapshot["sl_enabled"] = self.sl_enabled
            self._snapshot["sl_hits"] = self.sl_hits
            self._snapshot["sl_to_dry"] = self.sl_to_dry
        print(
            f"SL → {'ON' if self.sl_enabled else 'OFF'} {self.symbol}",
            flush=True,
        )
        return {
            "ok": True,
            "sl_enabled": self.sl_enabled,
            "sl_hits": self.sl_hits,
            "sl_to_dry": self.sl_to_dry,
        }

    def manual_close(self) -> dict[str, Any]:
        """Market-close open position from the UI (paper or LIVE)."""
        if not self._paper:
            return {"ok": False, "error": "no open position"}
        mid = float((self._snapshot or {}).get("mid") or 0)
        if mid <= 0 and self._trail:
            mid = float(self._trail[-1]["mid"])
        if mid <= 0:
            mid = float(self._paper.get("entry") or 0)
        if mid <= 0:
            return {"ok": False, "error": "no mark price"}
        now = time.time()
        self._close_paper(mid, now, "manual")
        if self._paper is not None:
            return {
                "ok": False,
                "error": self._last_order_error or "close failed",
            }
        with self._lock:
            self._snapshot["paper"] = {}
            self._snapshot["trades"] = list(self._trades)
            self._snapshot["markers"] = list(self._markers)
            self._snapshot["session"] = self._session_view(0.0)
            self._snapshot["signal"] = self._signal
            self._snapshot["block_reason"] = self._block_reason
        return {"ok": True, "why": "manual", "mark": mid}

    def _pos_is_live(self, pos: dict[str, Any] | None = None) -> bool:
        p = pos if pos is not None else self._paper
        return bool(p) and str(p.get("source") or "") == "live"

    def _fetch_exchange_position(self) -> dict[str, Any] | None:
        """Open Binance Futures position for this symbol, if any."""
        if not self.api_key or not self.api_secret:
            return None
        from orderbook_dca_grid import get_position_meta

        long_m = get_position_meta(
            self.symbol, True, self.hedge, self.api_key, self.api_secret, self.recv_window
        )
        short_m = get_position_meta(
            self.symbol, False, self.hedge, self.api_key, self.api_secret, self.recv_window
        )
        if float(long_m["qty"]) > 0:
            return {
                "side": "long",
                "qty": float(long_m["qty"]),
                "entry": float(long_m["entry"]),
                "upnl": float(long_m["unrealized_pnl"]),
            }
        if float(short_m["qty"]) > 0:
            return {
                "side": "short",
                "qty": float(short_m["qty"]),
                "entry": float(short_m["entry"]),
                "upnl": float(short_m["unrealized_pnl"]),
            }
        return None

    def _adopt_exchange_position(self, now: float) -> dict[str, Any] | None:
        """Bind bot management to an already-open exchange position."""
        ex = self._fetch_exchange_position()
        if not ex:
            return None
        side = str(ex["side"])
        entry = float(ex["entry"])
        qty = float(ex["qty"])
        if entry <= 0 or qty <= 0:
            return None
        mid = float((self._snapshot or {}).get("mid") or entry)
        bid_walls: list[dict[str, float]] = []
        ask_walls: list[dict[str, float]] = []
        best_bid = best_ask = None
        try:
            book = self._fetch_book()
            mid = float(book.get("mid") or mid)
            bid_walls = list(book.get("bid_walls") or [])
            ask_walls = list(book.get("ask_walls") or [])
            best_bid = book.get("best_bid")
            best_ask = book.get("best_ask")
        except Exception:  # noqa: BLE001
            pass
        wall_price = entry
        if side == "long" and bid_walls:
            wall_price = float(bid_walls[0]["price"])
        elif side == "short" and ask_walls:
            wall_price = float(ask_walls[0]["price"])
        if self.exits == "wall":
            tp, sl, exits_note = self._wall_tp_sl(
                side, entry, mid, bid_walls, ask_walls, best_bid, best_ask
            )
        else:
            tp, sl = self._pct_tp_sl(side, entry)
            exits_note = "pct"
        self._paper = {
            "side": side,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "wall_price": wall_price,
            "opened_at": now,
            "source": "live",
            "reason": "adopt",
            "exits": f"adopt+{exits_note}",
            "qty": qty,
            "base_qty": qty,
            "qty_str": f"{qty:.6g}",
            "order_id": "",
            "pnl_pct": self._pnl_pct(entry, mid, side),
            "peak_pnl_pct": 0.0,
            "peak_mid": mid,
            "armed": False,
            "dca_count": 0,
            "dca_walls": [float(wall_price)],
            "adverse_mid": mid,
            "be_locked": False,
        }
        self._last_order_error = None
        self._session["notional"] = qty * entry
        self._markers.append(
            {
                "t": int(now),
                "side": side,
                "kind": "entry",
                "label": "A",
                "win": True,
            }
        )
        if len(self._markers) > 80:
            self._markers = self._markers[-80:]
        print(
            f"ADOPT {side.upper()} {self.symbol} qty={qty:g} @ {entry:g} "
            f"TP {tp:g} SL {sl:g}",
            flush=True,
        )
        return {
            "side": side,
            "qty": qty,
            "entry": entry,
            "tp": tp,
            "sl": sl,
        }

    def set_live(self, enabled: bool) -> dict[str, Any]:
        """Toggle LIVE vs PAPER without closing. LIVE adopts an exchange position if open."""
        want_live = bool(enabled)
        adopted: dict[str, Any] | None = None
        note = ""
        if want_live:
            if not self.api_key or not self.api_secret:
                return {"ok": False, "error": "API keys required for LIVE (.env)"}
            if self.filt is None:
                try:
                    from orderbook_dca_grid import _resolve_hedge, load_symbol_filters

                    self.filt = load_symbol_filters(self.symbol)
                    ns = argparse.Namespace(
                        position_mode="auto", recv_window=self.recv_window
                    )
                    self.hedge = _resolve_hedge(ns, self.api_key, self.api_secret)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": f"live setup failed: {exc}"}
            self.dry_run = False
            self.live_enabled = True
            self.sl_hits = 0  # fresh LIVE session for SL→DRY counter
            now = time.time()
            try:
                ex = self._fetch_exchange_position()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"position check failed: {exc}"}
            if ex:
                local = self._paper
                same = (
                    local
                    and str(local.get("side")) == ex["side"]
                    and self._pos_is_live(local)
                )
                if same:
                    # Refresh qty/avg from exchange; keep TP/SL management
                    local["qty"] = float(ex["qty"])
                    local["entry"] = float(ex["entry"])
                    local["base_qty"] = float(local.get("base_qty") or ex["qty"])
                    note = "synced live position"
                else:
                    adopted = self._adopt_exchange_position(now)
                    note = "adopted exchange position" if adopted else "adopt failed"
            elif self._paper and not self._pos_is_live(self._paper):
                note = "kept paper position; new entries LIVE"
            else:
                note = "LIVE armed (flat)"
            print(f"MODE → LIVE {self.symbol} · {note}", flush=True)
        else:
            self.dry_run = True
            self.live_enabled = bool(self.api_key and self.api_secret)
            if self._pos_is_live():
                note = "PAPER mode; still managing open LIVE position until flat"
            elif self._paper:
                note = "PAPER mode; managing paper position"
            else:
                note = "PAPER armed (flat)"
            print(f"MODE → PAPER {self.symbol} · {note}", flush=True)
        with self._lock:
            self._snapshot["dry_run"] = self.dry_run
            self._snapshot["live_enabled"] = self.live_enabled
            self._snapshot["sl_hits"] = self.sl_hits
            self._snapshot["sl_to_dry"] = self.sl_to_dry
            self._snapshot["paper"] = dict(self._paper) if self._paper else {}
            self._snapshot["trades"] = list(self._trades)
            self._snapshot["markers"] = list(self._markers)
            unreal = float((self._paper or {}).get("pnl_pct") or 0)
            self._snapshot["session"] = self._session_view(unreal)
            self._snapshot["signal"] = self._signal
            self._snapshot["block_reason"] = self._block_reason
        return {
            "ok": True,
            "live": not self.dry_run,
            "dry_run": self.dry_run,
            "adopted": adopted is not None,
            "adopt": adopted,
            "note": note,
            "sl_hits": self.sl_hits,
            "sl_to_dry": self.sl_to_dry,
        }

    def filter_flags(self) -> dict[str, bool]:
        return {
            "bb": bool(self.bb_filter),
            "ema": bool(self.ema_filter),
            "book": bool(self.book_filter),
            "mom": bool(self.mom_filter),
            "bounce": bool(self.require_bounce),
            "ratio": bool(self.ratio_filter),
            "conf": bool(self.conf_filter),
            "bb_strict": bool(self.bb_strict),
            "dca": bool(self.dca_enabled),
            "obtp": bool(self.ob_tp_exit),
            "sl": bool(self.sl_enabled),
            "rsi": bool(self.rsi_filter),
            "adx": bool(self.adx_filter),
            "smc": bool(self.smc_filter),
        }

    def _fee_cover_need(self, pos: dict[str, Any] | None = None) -> float:
        """Gross %% needed to cover estimated fees (scales with DCA legs)."""
        dca_n = int((pos or self._paper or {}).get("dca_count") or 0)
        return self.fee_rt_pct * (2 + dca_n) / 2.0

    def _nearest_reward_ob(
        self,
        side: str,
        entry: float,
        mid: float,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
    ) -> dict[str, float] | None:
        """Nearest significant opposite OB wall still in profit direction from avg."""
        walls = ask_walls if side == "long" else bid_walls
        cands: list[dict[str, float]] = []
        for w in walls:
            if w["notional"] < self.min_wall_usdt:
                continue
            px = float(w["price"])
            if side == "long" and px <= entry:
                continue
            if side == "short" and px >= entry:
                continue
            cands.append(w)
        if not cands:
            return None
        # Prefer walls near current mid (where price actually is)
        near = [
            w
            for w in cands
            if mid > 0 and abs(float(w["price"]) - mid) / mid * 100 <= max(self.touch_pct * 2, 0.15)
        ]
        pool = near if near else cands
        return min(pool, key=lambda w: abs(float(w["price"]) - mid))

    def _exchange_flat(self) -> bool:
        if self.dry_run or not self.api_key:
            return True
        try:
            from orderbook_dca_grid import get_position_meta

            long_m = get_position_meta(
                self.symbol, True, self.hedge, self.api_key, self.api_secret, self.recv_window
            )
            short_m = get_position_meta(
                self.symbol, False, self.hedge, self.api_key, self.api_secret, self.recv_window
            )
            return float(long_m["qty"]) <= 0 and float(short_m["qty"]) <= 0
        except Exception:
            return False

    def _size_order(self, price: float) -> tuple[str, float]:
        if not self.filt:
            raise RuntimeError("symbol filters not loaded")
        if self.notional <= 0:
            return qty_exchange_min(price, self.filt)
        return qty_for_notional(self.notional, price, self.filt)

    def _session_view(self, unrealized_pct: float = 0.0) -> dict[str, Any]:
        net = float(self._session["net_pct"])
        self._session["fee_rt_pct"] = self.fee_rt_pct
        return {
            **self._session,
            "unrealized_pct": unrealized_pct,
            "total_pct": net + unrealized_pct,
        }

    def _refresh_ema(self, now: float) -> None:
        if now < self._ema_next:
            return
        self._ema_next = now + 10.0
        try:
            from ob_ema import fetch_ema_snapshot

            snap = fetch_ema_snapshot(
                self.symbol,
                interval=self.ema_interval,
                fast=self.ema_fast,
                slow=self.ema_slow,
                slope_min_pct=self.ema_slope_min,
            )
            self._ema_snap = snap
            if snap is None:
                self._trend = {"label": "warmup", "detail": "ema warmup"}
                return
            gate = "on" if self.ema_filter else "off"
            self._trend = {
                "label": snap.trend,
                "detail": (
                    f"ema{snap.fast_period}/{snap.slow_period} {self.ema_interval} "
                    f"slope {snap.slope_pct:+.3f}%  "
                    f"allow L={snap.allow_long} S={snap.allow_short}  "
                    f"filter={gate}"
                ),
                "slope_pct": snap.slope_pct,
                "allow_long": snap.allow_long,
                "allow_short": snap.allow_short,
            }
        except Exception as exc:  # noqa: BLE001
            self._ema_snap = None
            self._trend = {"label": "ema-err", "detail": str(exc)}

    def _refresh_osc(self, now: float) -> None:
        if now < self._osc_next:
            return
        self._osc_next = now + 20.0
        try:
            from ob_oscillators import OscillatorConfig, fetch_oscillators

            snap = fetch_oscillators(
                self.symbol,
                OscillatorConfig(
                    interval=self.osc_interval,
                    rsi_oversold=self.rsi_oversold,
                    rsi_overbought=self.rsi_overbought,
                    adx_period=self.adx_period,
                    adx_min=self.adx_min,
                ),
            )
            self._osc_snap = snap
            self._osc = {
                "rsi": snap.rsi,
                "adx": snap.adx,
                "di_plus": snap.di_plus,
                "di_minus": snap.di_minus,
                "rsi_side": snap.rsi_side,
                "adx_side": snap.adx_side,
                "interval": snap.interval,
                "detail": snap.reason,
            }
        except Exception as exc:  # noqa: BLE001
            self._osc_snap = None
            self._osc = {
                "rsi": None,
                "adx": None,
                "di_plus": None,
                "di_minus": None,
                "rsi_side": "none",
                "adx_side": "none",
                "detail": str(exc),
            }

    def _refresh_structure(self, now: float) -> None:
        if now < self._struct_next:
            return
        self._struct_next = now + 25.0
        try:
            from ob_structure import StructureConfig, fetch_structure

            snap = fetch_structure(
                self.symbol,
                StructureConfig(interval=self.structure_interval),
            )
            self._struct_snap = snap
            label = "flat"
            if snap.choch != "none":
                label = f"choch-{snap.choch}"
            elif snap.eql:
                label = "eql"
            elif snap.eqh:
                label = "eqh"
            self._structure = {
                "label": label,
                "choch": snap.choch,
                "eqh": bool(snap.eqh),
                "eql": bool(snap.eql),
                "allow_long": bool(snap.allow_long),
                "allow_short": bool(snap.allow_short),
                "eqh_level": snap.eqh_level,
                "eql_level": snap.eql_level,
                "interval": snap.interval,
                "detail": snap.reason,
                "reason": snap.reason,
            }
        except Exception as exc:  # noqa: BLE001
            self._struct_snap = None
            self._structure = {
                "label": "smc-err",
                "choch": "none",
                "eqh": False,
                "eql": False,
                "allow_long": False,
                "allow_short": False,
                "detail": str(exc),
                "reason": str(exc),
            }

    def _rsi_allows(self, side: str) -> bool:
        """Block entries into RSI extremes against the side."""
        if not self.rsi_filter:
            return True
        rsi = self._osc.get("rsi")
        if rsi is None:
            return True
        if side == "long" and float(rsi) >= self.rsi_overbought:
            return False
        if side == "short" and float(rsi) <= self.rsi_oversold:
            return False
        return True

    def _adx_allows(self, side: str) -> bool:
        """Require trend strength + DI alignment when ADX filter is on."""
        if not self.adx_filter:
            return True
        adx = self._osc.get("adx")
        if adx is None:
            return True
        if float(adx) < self.adx_min:
            return False
        bias = str(self._osc.get("adx_side") or "none")
        if bias == "none":
            return False
        return bias == side

    def _smc_allows(self, side: str) -> bool:
        """When SMC filter on: block clear opposing structure bias."""
        if not self.smc_filter:
            return True
        st = self._structure or {}
        allow_l = bool(st.get("allow_long"))
        allow_s = bool(st.get("allow_short"))
        if side == "long" and allow_s and not allow_l:
            return False
        if side == "short" and allow_l and not allow_s:
            return False
        return True

    def _recent_mom_pct(self) -> float:
        """Short-horizon mid momentum %% from the live trail (~last few samples)."""
        trail = list(self._trail)
        if len(trail) < 3:
            return 0.0
        a = float(trail[-3]["mid"])
        b = float(trail[-1]["mid"])
        if a <= 0:
            return 0.0
        return (b - a) / a * 100

    def _update_book_pressure(
        self,
        bids: list[list[float]],
        asks: list[list[float]],
        mid: float,
        *,
        levels: int = 28,
    ) -> dict[str, Any]:
        """Distance-weighted bid/ask notional from the depth ladder → micro book trend."""
        if mid <= 0:
            self._book = {"label": "n/a", "pressure": 0.5, "trend": "n/a", "detail": ""}
            return self._book

        bid_raw = ask_raw = 0.0
        bid_w = ask_w = 0.0
        for price, qty in bids[:levels]:
            n = price * qty
            dist = abs(price - mid) / mid * 100
            w = 1.0 / (1.0 + dist * 2.5)  # closer levels weigh more
            bid_raw += n
            bid_w += n * w
        for price, qty in asks[:levels]:
            n = price * qty
            dist = abs(price - mid) / mid * 100
            w = 1.0 / (1.0 + dist * 2.5)
            ask_raw += n
            ask_w += n * w

        total_w = bid_w + ask_w
        pressure = (bid_w / total_w) if total_w > 0 else 0.5
        self._book_hist.append(pressure)

        if pressure >= 0.57:
            label = "bid-heavy"
        elif pressure <= 0.43:
            label = "ask-heavy"
        else:
            label = "balanced"

        hist = list(self._book_hist)
        if len(hist) >= 8:
            delta = hist[-1] - hist[-8]
            if delta >= 0.025:
                btrend = "building-bid"
            elif delta <= -0.025:
                btrend = "building-ask"
            else:
                btrend = "stable"
        else:
            btrend = "warmup"

        self._book = {
            "label": label,
            "pressure": round(pressure, 4),
            "trend": btrend,
            "bid_usdt": round(bid_raw, 0),
            "ask_usdt": round(ask_raw, 0),
            "detail": (
                f"ladder bid {bid_raw/1000:.0f}k / ask {ask_raw/1000:.0f}k USDT  "
                f"w-pressure {pressure*100:.1f}% bid  {btrend}"
            ),
        }
        return self._book

    def _score_confidence(
        self,
        side: str,
        *,
        mid: float,
        imb: float,
        entry_wall: dict[str, float],
        opp_wall: dict[str, float] | None,
        tp_wall: dict[str, float] | None,
        spread: float,
        mom_pct: float,
    ) -> dict[str, Any]:
        """0–100 confidence for a candidate entry."""
        parts: dict[str, float] = {}

        # Imbalance strength beyond threshold (0–18) — near-mid band
        if side == "long":
            excess = max(0.0, imb - self.imb_long)
            span = max(1e-6, 1.0 - self.imb_long)
        else:
            excess = max(0.0, self.imb_short - imb)
            span = max(1e-6, self.imb_short)
        parts["imb"] = min(18.0, (excess / span) * 18.0)

        # Entry wall size vs min (0–16)
        wall_n = float(entry_wall.get("notional") or 0)
        parts["wall"] = min(16.0, (wall_n / max(self.min_wall_usdt, 1.0)) * 8.0)

        # Wall dominance vs opposite (0–12)
        opp_n = float((opp_wall or {}).get("notional") or 0)
        ratio = wall_n / opp_n if opp_n > 0 else 0.0
        if ratio >= max(self.min_wall_ratio, 1.0) * 1.5:
            parts["ratio"] = 12.0
        elif ratio >= max(self.min_wall_ratio, 1.0):
            parts["ratio"] = 8.0
        elif ratio >= 1.0:
            parts["ratio"] = 4.0
        else:
            parts["ratio"] = 0.0

        # Full ladder book pressure (0–14) — uses depth profile stack
        bp = float(self._book.get("pressure") or 0.5)
        btr = str(self._book.get("trend") or "")
        if side == "long":
            parts["book"] = max(0.0, min(14.0, (bp - 0.45) / 0.25 * 14.0))
            if btr == "building-bid":
                parts["book"] = min(14.0, parts["book"] + 3.0)
            elif btr == "building-ask":
                parts["book"] = max(0.0, parts["book"] - 4.0)
        else:
            parts["book"] = max(0.0, min(14.0, (0.55 - bp) / 0.25 * 14.0))
            if btr == "building-ask":
                parts["book"] = min(14.0, parts["book"] + 3.0)
            elif btr == "building-bid":
                parts["book"] = max(0.0, parts["book"] - 4.0)

        # BB regime (0–12) — prefer clean range
        label = str(self._bb.get("label") or "")
        parts["bb"] = {"range": 12.0, "wide": 10.0, "near-bb": 3.0}.get(label, 0.0)

        # EMA alignment (0–14)
        snap = self._ema_snap
        if snap is None:
            parts["ema"] = 7.0 if not self.ema_filter else 0.0
        elif side == "long" and snap.allow_long:
            parts["ema"] = 14.0
        elif side == "short" and snap.allow_short:
            parts["ema"] = 14.0
        elif snap.trend == "flat":
            parts["ema"] = 3.0
        else:
            parts["ema"] = 0.0  # counter-trend

        # Momentum aligned with side (0–10)
        if side == "long":
            if mom_pct >= 0.01:
                parts["mom"] = min(10.0, 5.0 + mom_pct * 80)
            elif mom_pct >= -self.mom_max_against:
                parts["mom"] = 3.0
            else:
                parts["mom"] = 0.0
        else:
            if mom_pct <= -0.01:
                parts["mom"] = min(10.0, 5.0 + abs(mom_pct) * 80)
            elif mom_pct <= self.mom_max_against:
                parts["mom"] = 3.0
            else:
                parts["mom"] = 0.0

        # TP room beyond fee+edge (0–10)
        need = self._min_tp_dist_pct()
        if tp_wall and mid > 0:
            dist = abs(float(tp_wall["price"]) - mid) / mid * 100
            parts["tp"] = min(10.0, max(0.0, (dist - need) / max(need, 0.01) * 5.0 + 5.0))
        else:
            parts["tp"] = 0.0

        spread_pct = (spread / mid * 100) if mid > 0 else 0.0
        if spread_pct > 0.05:
            parts["tp"] = max(0.0, parts["tp"] - 3.0)

        score = min(100.0, sum(parts.values()))
        return {
            "score": round(score, 1),
            "min": self.min_confidence,
            "side": side,
            "parts": {k: round(v, 1) for k, v in parts.items()},
            "wall_ratio": round(ratio, 2),
            "mom_pct": round(mom_pct, 4),
        }

    def _refresh_fees(self, now: float) -> None:
        if now < self._fee_next:
            return
        self._fee_next = now + 300.0  # refresh every 5 min
        if not self.api_key or not self.api_secret:
            self._session["fee_source"] = "fallback"
            self._session["fee_rt_pct"] = self.fee_fallback_pct
            self.fee_rt_pct = self.fee_fallback_pct
            return
        try:
            rates = fetch_commission_rates(
                self.symbol, self.api_key, self.api_secret, self.recv_window
            )
            maker = rates["maker_pct"]
            taker = rates["taker_pct"]
            rt = round_trip_fee_pct(maker, taker, self.fee_mode)
            self.fee_rt_pct = rt
            self._session["fee_source"] = "binance"
            self._session["fee_mode"] = self.fee_mode
            self._session["maker_pct"] = maker
            self._session["taker_pct"] = taker
            self._session["fee_rt_pct"] = rt
        except Exception as exc:  # noqa: BLE001
            self._session["fee_source"] = "fallback"
            self._session["fee_error"] = str(exc)
            self.fee_rt_pct = self.fee_fallback_pct
            self._session["fee_rt_pct"] = self.fee_fallback_pct

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

    def _depth_profile(
        self,
        bids: list[list[float]],
        asks: list[list[float]],
        mid: float,
        *,
        levels: int = 28,
        wall_usdt: float | None = None,
    ) -> list[dict[str, Any]]:
        """Horizontal depth ladder for UI (asks above mid, bids below)."""
        min_wall = self.min_wall_usdt if wall_usdt is None else wall_usdt
        ask_rows = []
        for price, qty in asks[:levels]:
            notional = price * qty
            ask_rows.append(
                {
                    "side": "ask",
                    "price": price,
                    "qty": qty,
                    "notional": notional,
                    "wall": notional >= min_wall,
                }
            )
        bid_rows = []
        for price, qty in bids[:levels]:
            notional = price * qty
            bid_rows.append(
                {
                    "side": "bid",
                    "price": price,
                    "qty": qty,
                    "notional": notional,
                    "wall": notional >= min_wall,
                }
            )
        # Top = highest ask … down to mid … then best bid down
        ask_rows.sort(key=lambda r: r["price"], reverse=True)
        mid_row = {"side": "mid", "price": mid, "qty": 0.0, "notional": 0.0, "wall": False}
        return ask_rows + [mid_row] + bid_rows

    def _bb_regime_for_mid(self, mid: float) -> dict[str, Any]:
        """Re-score BB every tick vs live mid (bands may be cached)."""
        upper = self._bb.get("upper")
        lower = self._bb.get("lower")
        mid_bb = self._bb.get("mid")
        if upper is None or lower is None or mid_bb is None:
            return {
                **self._bb,
                "tradeable": self._bb.get("label") not in ("warmup",),
                "live_mid": mid,
            }
        pad = mid * (self.bb_pad_pct / 100.0)
        inside = lower <= mid <= upper
        near = (lower - pad) <= mid <= (upper + pad)
        width_pct = float(self._bb.get("width_pct") or 0.0)
        tight = width_pct < 2.5 if mid >= 1000 else width_pct < 4.0
        if inside:
            label = "range" if tight else "wide"
        elif near:
            label = "near-bb"
        else:
            label = "breakout"
        # Strict: only inside BB. Loose: inside or pad (near-bb).
        tradeable = inside if self.bb_strict else near
        return {
            **self._bb,
            "label": label,
            "tradeable": tradeable,
            "live_mid": mid,
            "inside": inside,
            "near": near,
        }

    def _refresh_bb(self, now: float, mid: float) -> None:
        if now < self._bb_next:
            # Keep label/tradeable fresh even between kline fetches
            if self._bb.get("upper") is not None:
                self._bb = self._bb_regime_for_mid(mid)
            return
        self._bb_next = now + 15.0
        try:
            kl = fetch_klines(self.symbol, self.bb_interval, self.bb_period + 5)
            closes = [float(k[4]) for k in kl]
            self._closes = closes
            bb = bollinger(closes, self.bb_period, self.bb_std)
            if not bb:
                self._bb = {"label": "warmup", "tradeable": False}
                return
            upper, mid_bb, lower = bb
            width_pct = (upper - lower) / mid_bb * 100 if mid_bb else 0.0
            self._bb = {
                "upper": upper,
                "mid": mid_bb,
                "lower": lower,
                "width_pct": width_pct,
            }
            self._bb = self._bb_regime_for_mid(mid)
            t_sec = int(now)
            pt = {"t": t_sec, "upper": upper, "mid": mid_bb, "lower": lower}
            if self._bb_trail and self._bb_trail[-1]["t"] == t_sec:
                self._bb_trail[-1] = pt
            else:
                self._bb_trail.append(pt)
            cutoff = now - self.trail_sec
            while self._bb_trail and self._bb_trail[0]["t"] < cutoff:
                self._bb_trail.popleft()
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            self._bb = {"label": "bb-err", "tradeable": True, "error": str(exc)}

    def _refresh_live(self, now: float) -> None:
        if not self.live_enabled:
            self._live = {}
            return
        if now < self._live_next:
            return
        self._live_next = now + 3.0
        if not self.api_key or not self.api_secret:
            self._live = {"error": "no API keys in .env"}
            return
        try:
            from orderbook_dca_grid import get_position_meta

            long_m = get_position_meta(
                self.symbol, True, self.hedge, self.api_key, self.api_secret, self.recv_window
            )
            short_m = get_position_meta(
                self.symbol, False, self.hedge, self.api_key, self.api_secret, self.recv_window
            )
            if float(long_m["qty"]) > 0:
                self._live = {
                    "side": "long",
                    "qty": float(long_m["qty"]),
                    "entry": float(long_m["entry"]),
                    "upnl": float(long_m["unrealized_pnl"]),
                }
            elif float(short_m["qty"]) > 0:
                self._live = {
                    "side": "short",
                    "qty": float(short_m["qty"]),
                    "entry": float(short_m["entry"]),
                    "upnl": float(short_m["unrealized_pnl"]),
                }
            else:
                self._live = {}
        except Exception as exc:  # noqa: BLE001 — surface any key/API issue in UI
            self._live = {"error": str(exc)}

    def _pnl_pct(self, entry: float, mark: float, side: str) -> float:
        if entry <= 0:
            return 0.0
        if side == "long":
            return (mark - entry) / entry * 100
        return (entry - mark) / entry * 100

    def _min_tp_dist_pct(self) -> float:
        """Minimum reward to opposite wall so a win can cover fees + edge."""
        return self.fee_rt_pct + self.min_edge_pct

    def _covers_fees(self, gross_pct: float) -> bool:
        return gross_pct >= self.fee_rt_pct

    def _min_sl_dist_pct(self) -> float:
        """Min adverse SL distance from entry (noise room; covers a bounce)."""
        return max(self.sl_pct, self.min_sl_pct)

    def _pct_tp_sl(self, side: str, entry: float) -> tuple[float, float]:
        # Prefer fee-aware distance over tiny legacy tp_pct
        tp_d = max(self.tp_pct, self._min_tp_dist_pct())
        sl_d = self._min_sl_dist_pct()
        if side == "long":
            return entry * (1 + tp_d / 100), entry * (1 - sl_d / 100)
        return entry * (1 - tp_d / 100), entry * (1 + sl_d / 100)

    def _reward_wall(
        self,
        walls: list[dict[str, float]],
        *,
        above: bool,
        mid: float,
        min_dist_pct: float,
    ) -> dict[str, float] | None:
        """Nearest significant wall at least min_dist_pct away (farther target)."""
        cands: list[dict[str, float]] = []
        for w in walls:
            if w["notional"] < self.min_wall_usdt:
                continue
            if above and w["price"] <= mid:
                continue
            if not above and w["price"] >= mid:
                continue
            dist = abs(w["price"] - mid) / mid * 100 if mid else 0.0
            if dist >= min_dist_pct:
                cands.append(w)
        if not cands:
            return None
        return min(cands, key=lambda w: abs(w["price"] - mid))

    def _stop_wall(
        self,
        walls: list[dict[str, float]],
        *,
        above: bool,
        entry: float,
        min_dist_pct: float,
    ) -> dict[str, float] | None:
        """Nearest significant wall at least min_dist_pct beyond entry (SL side)."""
        if entry <= 0:
            return None
        cands: list[dict[str, float]] = []
        for w in walls:
            if w["notional"] < self.min_wall_usdt:
                continue
            if above and w["price"] <= entry:
                continue
            if not above and w["price"] >= entry:
                continue
            dist = abs(w["price"] - entry) / entry * 100
            if dist >= min_dist_pct:
                cands.append(w)
        if not cands:
            return None
        return min(cands, key=lambda w: abs(w["price"] - entry))

    def _wall_tp_sl(
        self,
        side: str,
        entry: float,
        mid: float,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
        best_bid: float | None,
        best_ask: float | None,
    ) -> tuple[float, float, str]:
        """Dynamic TP/SL: TP = opposite wall for fees; SL = structure beyond min room."""
        fallback_tp, fallback_sl = self._pct_tp_sl(side, entry)
        buf = self.sl_buffer_pct / 100
        need = self._min_tp_dist_pct()
        min_sl = self._min_sl_dist_pct()
        note = "wall"

        if side == "long":
            ask_w = self._reward_wall(ask_walls, above=True, mid=mid, min_dist_pct=need)
            if ask_w is None:
                ask_w = self._nearest_wall(ask_walls, below=False, mid=mid)
            if ask_w:
                tp = ask_w["price"]
                dist = abs(tp - entry) / entry * 100
                if dist < need:
                    tp = entry * (1 + need / 100)
                    note = "wall+min"
            elif best_ask and best_ask > mid:
                tp = max(best_ask, entry * (1 + need / 100))
                note = "wall+bbo"
            else:
                tp = fallback_tp
                note = "wall→pct"
            max_tp = entry * (1 + self.max_tp_pct / 100)
            if tp > max_tp:
                tp = max_tp
            if tp <= mid:
                tp = max(fallback_tp, mid * (1 + need / 200))

            # SL below entry by ≥ min_sl — never glue to best bid / mid
            bid_w = self._stop_wall(
                bid_walls, above=False, entry=entry, min_dist_pct=min_sl
            )
            floor_sl = entry * (1 - min_sl / 100)
            if bid_w:
                sl = bid_w["price"] * (1 - buf)
                note = note if "pct" in note else "wall+sl"
            else:
                sl = fallback_sl
                note = note if "pct" in note else "wall→sl-pct"
            sl = min(sl, floor_sl)
            return tp, sl, note

        # short
        bid_w = self._reward_wall(bid_walls, above=False, mid=mid, min_dist_pct=need)
        if bid_w is None:
            bid_w = self._nearest_wall(bid_walls, below=True, mid=mid)
        if bid_w:
            tp = bid_w["price"]
            dist = abs(entry - tp) / entry * 100
            if dist < need:
                tp = entry * (1 - need / 100)
                note = "wall+min"
        elif best_bid and best_bid < mid:
            tp = min(best_bid, entry * (1 - need / 100))
            note = "wall+bbo"
        else:
            tp = fallback_tp
            note = "wall→pct"
        min_tp = entry * (1 - self.max_tp_pct / 100)
        if tp < min_tp:
            tp = min_tp
        if tp >= mid:
            tp = min(fallback_tp, mid * (1 - need / 200))

        # SL above entry by ≥ min_sl — skip the ask wall used for entry
        ask_w = self._stop_wall(
            ask_walls, above=True, entry=entry, min_dist_pct=min_sl
        )
        ceil_sl = entry * (1 + min_sl / 100)
        if ask_w:
            sl = ask_w["price"] * (1 + buf)
            note = note if "pct" in note else "wall+sl"
        else:
            sl = fallback_sl
            note = note if "pct" in note else "wall→sl-pct"
        sl = max(sl, ceil_sl)
        return tp, sl, note

    def _open_paper(
        self,
        side: str,
        entry: float,
        wall_price: float,
        now: float,
        reason: str,
        *,
        bid_walls: list[dict[str, float]] | None = None,
        ask_walls: list[dict[str, float]] | None = None,
        best_bid: float | None = None,
        best_ask: float | None = None,
    ) -> None:
        fill_entry = entry
        qty = 0.0
        qty_str = ""
        source = "paper" if self.dry_run else "live"
        order_id = ""

        if not self.dry_run:
            if not self.api_key or not self.api_secret or not self.filt:
                self._block_reason = "live needs API keys + filters"
                self._last_order_error = self._block_reason
                return
            if not self._exchange_flat():
                self._block_reason = "exchange already has a position"
                self._last_order_error = self._block_reason
                return
            try:
                qty_str, qty = self._size_order(entry)
                cid = _cid("oblive", self.symbol, side)
                resp = market_open(
                    self.symbol,
                    side == "long",
                    qty_str,
                    self.hedge,
                    self.api_key,
                    self.api_secret,
                    self.recv_window,
                    cid=cid,
                )
                order_id = str(resp.get("orderId", "") or cid)
                # Prefer avg fill / position entry when available
                avg = float(resp.get("avgPrice", 0) or 0)
                if avg > 0:
                    fill_entry = avg
                else:
                    from orderbook_dca_grid import get_position_meta

                    meta = get_position_meta(
                        self.symbol,
                        side == "long",
                        self.hedge,
                        self.api_key,
                        self.api_secret,
                        self.recv_window,
                    )
                    if float(meta["entry"]) > 0:
                        fill_entry = float(meta["entry"])
                    if float(meta["qty"]) > 0:
                        qty = float(meta["qty"])
                print(
                    f"LIVE OPEN {side.upper()} {self.symbol} qty={qty_str} "
                    f"@ ~{fill_entry:g} cid={cid}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                self._block_reason = f"open failed: {exc}"
                self._last_order_error = str(exc)
                print(f"LIVE OPEN FAILED: {exc}", flush=True)
                return
        elif self.notional > 0 and fill_entry > 0:
            qty = self.notional / fill_entry
            qty_str = f"{qty:.6g}"
        if qty <= 0 and fill_entry > 0:
            # Paper unit size so DCA averaging works in dry-run
            qty = 1.0
            qty_str = "1"

        if self.exits == "wall":
            tp, sl, exits_note = self._wall_tp_sl(
                side, fill_entry, fill_entry, bid_walls or [], ask_walls or [], best_bid, best_ask
            )
        else:
            tp, sl = self._pct_tp_sl(side, fill_entry)
            exits_note = "pct"
        self._paper = {
            "side": side,
            "entry": fill_entry,
            "tp": tp,
            "sl": sl,
            "wall_price": wall_price,
            "opened_at": now,
            "source": source,
            "reason": reason,
            "exits": exits_note,
            "qty": qty,
            "base_qty": qty,
            "qty_str": qty_str,
            "order_id": order_id,
            "pnl_pct": 0.0,
            "peak_pnl_pct": 0.0,
            "peak_mid": fill_entry,
            "armed": False,
            "dca_count": 0,
            "dca_walls": [float(wall_price)],
            "adverse_mid": fill_entry,  # worst mid since entry (for DCA direction)
        }
        self._last_order_error = None
        if qty > 0 and fill_entry > 0:
            self._session["notional"] = qty * fill_entry
        self._markers.append(
            {
                "t": int(now),
                "side": side,
                "kind": "entry",
                "label": side.upper()[:1],
                "win": True,
            }
        )
        if len(self._markers) > 80:
            self._markers = self._markers[-80:]

    def _close_paper(self, mark: float, now: float, why: str) -> None:
        pos = self._paper
        if not pos:
            return
        side = pos["side"]
        # Exit on exchange only for adopted/opened LIVE legs (mode toggle must not orphan)
        if self._pos_is_live(pos) and self.api_key and self.filt:
            try:
                from orderbook_dca_grid import get_position_meta, market_close_position

                meta = get_position_meta(
                    self.symbol,
                    side == "long",
                    self.hedge,
                    self.api_key,
                    self.api_secret,
                    self.recv_window,
                )
                qty = float(meta["qty"]) or float(pos.get("qty") or 0)
                if qty > 0:
                    closed = market_close_position(
                        self.symbol,
                        side == "long",
                        qty,
                        self.hedge,
                        self.filt,
                        self.api_key,
                        self.api_secret,
                        self.recv_window,
                    )
                    print(
                        f"LIVE CLOSE {side.upper()} {self.symbol} qty={closed:g} "
                        f"why={why} @ ~{mark:g}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                self._last_order_error = str(exc)
                print(f"LIVE CLOSE FAILED: {exc}", flush=True)
                # Keep managing position until close succeeds
                self._block_reason = f"close failed: {exc}"
                return

        gross = self._pnl_pct(pos["entry"], mark, side)
        # N entry legs + 1 exit ≈ fee_rt * (entries + 1) / 2
        dca_n = int(pos.get("dca_count") or 0)
        fee = self.fee_rt_pct * (2 + dca_n) / 2.0
        net = gross - fee
        trade = {
            "side": side,
            "entry": pos["entry"],
            "exit": mark,
            "qty": float(pos.get("qty") or 0),
            "pnl_pct": gross,
            "fee_pct": fee,
            "net_pct": net,
            "why": why,
            "dca": dca_n,
            "source": pos.get("source", "paper"),
            "t": int(now),
        }
        self._trades.append(trade)
        if len(self._trades) > 50:
            self._trades = self._trades[-50:]
        self._session["trades"] = int(self._session["trades"]) + 1
        self._session["gross_pct"] = float(self._session["gross_pct"]) + gross
        self._session["fees_pct"] = float(self._session["fees_pct"]) + fee
        self._session["net_pct"] = float(self._session["net_pct"]) + net
        if net >= 0:
            self._session["wins"] = int(self._session["wins"]) + 1
        else:
            self._session["losses"] = int(self._session["losses"]) + 1
        self._markers.append(
            {
                "t": int(now),
                "side": side,
                "kind": "exit",
                "label": why[:3].upper(),
                "win": net >= 0,
            }
        )
        if len(self._markers) > 80:
            self._markers = self._markers[-80:]
        self._paper = None
        # After SL: longer cooldown; after N session SLs → force PAPER
        cd = self.cooldown_sec
        if why == "sl":
            cd = max(cd, 12.0)
            self.sl_hits += 1
        self._cooldown_until = now + cd
        self._signal = "flat"
        lim = int(self.sl_to_dry or 0)
        if why == "sl" and not self.dry_run and lim > 0 and self.sl_hits >= lim:
            self.set_live(False)
            self._block_reason = (
                f"cooldown {cd:g}s after sl · "
                f"{self.sl_hits}/{lim} SL → PAPER"
            )
        elif why == "sl":
            left = f" · {self.sl_hits}/{lim}→DRY" if lim > 0 else ""
            self._block_reason = f"cooldown {cd:g}s after sl{left}"
        else:
            self._block_reason = f"cooldown {cd:g}s after {why}"

    def _dca_space(self) -> float:
        return max(0.08, float(self.dca_space_pct), float(self.touch_pct) * 0.5)

    def _unused_adverse_walls(
        self,
        side: str,
        entry: float,
        walls: list[dict[str, float]],
        used: list[float],
    ) -> list[dict[str, float]]:
        space = self._dca_space()
        out: list[dict[str, float]] = []
        for w in walls:
            if w["notional"] < self.min_wall_usdt:
                continue
            px = float(w["price"])
            if side == "long":
                if px >= entry:
                    continue
            else:
                if px <= entry:
                    continue
            if any(u > 0 and abs(px - u) / u * 100 < space for u in used):
                continue
            out.append(w)
        return out

    def _further_adverse_wall(
        self,
        side: str,
        cand_px: float,
        walls: list[dict[str, float]],
    ) -> dict[str, float] | None:
        """Unused adverse wall further against the position than cand_px."""
        space = self._dca_space()
        further: list[dict[str, float]] = []
        for w in walls:
            px = float(w["price"])
            if side == "long" and px < cand_px * (1 - space / 100):
                further.append(w)
            if side == "short" and px > cand_px * (1 + space / 100):
                further.append(w)
        if not further:
            return None
        # Furthest against us
        if side == "long":
            return min(further, key=lambda w: float(w["price"]))
        return max(further, key=lambda w: float(w["price"]))

    def _next_dca_wall(
        self,
        side: str,
        entry: float,
        mid: float,
        walls: list[dict[str, float]],
        used: list[float],
    ) -> dict[str, float] | None:
        """Nearest unused adverse OB wall in touch — only while still extending."""
        if mid <= 0:
            return None
        cands = [
            w
            for w in self._unused_adverse_walls(side, entry, walls, used)
            if abs(float(w["price"]) - mid) / mid * 100 <= self.touch_pct
        ]
        if not cands:
            return None
        return min(cands, key=lambda w: abs(float(w["price"]) - mid))

    def _dca_still_adverse(self, side: str, mid: float, pos: dict[str, Any]) -> bool:
        """True only while price is still extending against us (retracement), not recovering."""
        adv = float(pos.get("adverse_mid") or mid)
        if side == "long":
            pos["adverse_mid"] = min(adv, mid)
        else:
            pos["adverse_mid"] = max(adv, mid)
        adv = float(pos["adverse_mid"])
        if adv <= 0 or mid <= 0:
            return False
        # Recovered from worst print by more than a tick of noise
        recover_pct = max(0.02, float(self.dca_space_pct) * 0.25)
        if side == "long" and mid > adv * (1 + recover_pct / 100):
            return False
        if side == "short" and mid < adv * (1 - recover_pct / 100):
            return False
        mom = self._recent_mom_pct()
        # Momentum must still be adverse (or flat); block bounce recovery
        if side == "long" and mom > 0.01:
            return False
        if side == "short" and mom < -0.01:
            return False
        return True

    def _add_dca(
        self,
        fill: float,
        wall: dict[str, float],
        now: float,
        *,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
        best_bid: float | None,
        best_ask: float | None,
    ) -> bool:
        """Add size at wall, rebase avg entry, recalculate TP/SL."""
        pos = self._paper
        if not pos:
            return False
        side = pos["side"]
        old_entry = float(pos["entry"])
        old_qty = float(pos.get("qty") or 0)
        add_qty = float(pos.get("base_qty") or old_qty or 0)
        if add_qty <= 0:
            add_qty = 1.0

        fill_px = fill
        use_exchange_avg = False
        if self._pos_is_live(pos):
            if not self.api_key or not self.api_secret or not self.filt:
                return False
            try:
                qty_str, add_qty = self._size_order(fill)
                cid = _cid("obdca", self.symbol, side)
                resp = market_open(
                    self.symbol,
                    side == "long",
                    qty_str,
                    self.hedge,
                    self.api_key,
                    self.api_secret,
                    self.recv_window,
                    cid=cid,
                )
                avg = float(resp.get("avgPrice", 0) or 0)
                if avg > 0:
                    fill_px = avg
                from orderbook_dca_grid import get_position_meta

                meta = get_position_meta(
                    self.symbol,
                    side == "long",
                    self.hedge,
                    self.api_key,
                    self.api_secret,
                    self.recv_window,
                )
                if float(meta["entry"]) > 0 and float(meta["qty"]) > 0:
                    pos["entry"] = float(meta["entry"])
                    pos["qty"] = float(meta["qty"])
                    use_exchange_avg = True
                print(
                    f"LIVE DCA {side.upper()} {self.symbol} +{add_qty:g} "
                    f"@ ~{fill_px:g}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                self._last_order_error = f"dca failed: {exc}"
                print(f"LIVE DCA FAILED: {exc}", flush=True)
                return False

        if not use_exchange_avg:
            new_qty = old_qty + add_qty
            new_avg = (
                (old_entry * old_qty + fill_px * add_qty) / new_qty
                if new_qty > 0
                else fill_px
            )
            pos["entry"] = new_avg
            pos["qty"] = new_qty
        new_avg = float(pos["entry"])
        new_qty = float(pos["qty"])

        pos["dca_count"] = int(pos.get("dca_count") or 0) + 1
        used = list(pos.get("dca_walls") or [])
        used.append(float(wall["price"]))
        pos["dca_walls"] = used
        pos["last_dca_at"] = now
        pos["wall_price"] = float(wall["price"])
        pos["armed"] = False
        pos["be_locked"] = False
        pos["peak_pnl_pct"] = 0.0
        pos["peak_mid"] = new_avg

        if self.exits == "wall":
            tp, sl, note = self._wall_tp_sl(
                side, new_avg, fill_px, bid_walls, ask_walls, best_bid, best_ask
            )
        else:
            tp, sl = self._pct_tp_sl(side, new_avg)
            note = "pct"
        pos["tp"] = tp
        pos["sl"] = sl  # full reset after DCA (not ratchet-tight)
        pos["exits"] = f"dca{pos['dca_count']}+{note}"
        if new_qty > 0 and new_avg > 0:
            self._session["notional"] = new_qty * new_avg
        self._markers.append(
            {
                "t": int(now),
                "side": side,
                "kind": "entry",
                "label": "D",
                "win": True,
            }
        )
        if len(self._markers) > 80:
            self._markers = self._markers[-80:]
        print(
            f"DCA #{pos['dca_count']} {side} +{add_qty:g} @ {fill_px:g} → "
            f"avg {new_avg:g}  TP {tp:g}  SL {sl:g}",
            flush=True,
        )
        return True

    def _maybe_dca(
        self,
        mid: float,
        now: float,
        pnl: float,
        *,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
        best_bid: float | None,
        best_ask: float | None,
    ) -> bool:
        pos = self._paper
        if not pos or not self.dca_enabled:
            return False
        if pnl >= -self.dca_min_loss_pct:
            return False
        if int(pos.get("dca_count") or 0) >= self.dca_max:
            return False
        last_dca = float(pos.get("last_dca_at") or 0)
        if last_dca > 0 and (now - last_dca) < self.dca_cooldown_sec:
            return False
        side = pos["side"]
        entry = float(pos["entry"])
        # Only DCA on adverse extension — never while recovering toward entry
        if not self._dca_still_adverse(side, mid, pos):
            return False
        used = [float(x) for x in (pos.get("dca_walls") or [])]
        walls = bid_walls if side == "long" else ask_walls
        unused = self._unused_adverse_walls(side, entry, walls, used)
        wall = self._next_dca_wall(side, entry, mid, walls, used)
        if wall is None:
            return False
        # Already extended past this wall toward a deeper OB → don't refill
        # the nearer one on the way back (wait for that deeper wall, or new lows/highs)
        cpx = float(wall["price"])
        further = self._further_adverse_wall(side, cpx, unused)
        if further is not None:
            space = self._dca_space()
            adv = float(pos.get("adverse_mid") or mid)
            if side == "short" and adv > cpx * (1 + space / 100):
                return False
            if side == "long" and adv < cpx * (1 - space / 100):
                return False
        return self._add_dca(
            mid, wall, now,
            bid_walls=bid_walls, ask_walls=ask_walls,
            best_bid=best_bid, best_ask=best_ask,
        )

    def _manage_paper(
        self,
        mid: float,
        now: float,
        imb: float,
        *,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
        best_bid: float | None,
        best_ask: float | None,
    ) -> None:
        if not self._paper:
            return
        pos = self._paper
        side = pos["side"]
        entry = float(pos["entry"])

        pnl = self._pnl_pct(entry, mid, side)
        pos["pnl_pct"] = pnl
        if pnl > float(pos.get("peak_pnl_pct", 0.0)):
            pos["peak_pnl_pct"] = pnl
        if side == "long":
            pos["peak_mid"] = max(float(pos.get("peak_mid", mid)), mid)
            pos["adverse_mid"] = min(float(pos.get("adverse_mid", mid)), mid)
        else:
            pos["peak_mid"] = min(float(pos.get("peak_mid", mid)), mid)
            pos["adverse_mid"] = max(float(pos.get("adverse_mid", mid)), mid)

        # DCA on adverse OB walls while underwater — rebase avg + TP/SL
        if self._maybe_dca(
            mid, now, pnl,
            bid_walls=bid_walls, ask_walls=ask_walls,
            best_bid=best_bid, best_ask=best_ask,
        ):
            entry = float(pos["entry"])
            pnl = self._pnl_pct(entry, mid, side)
            pos["pnl_pct"] = pnl

        held = now - float(pos.get("opened_at", now))
        in_grace = held < self.sl_grace_sec
        min_sl = self._min_sl_dist_pct()
        be_buf = max(0.0, self.be_buffer_pct)

        # Soft exits wait for fees; BE locks early once slightly green (after grace)
        fee_need = self._fee_cover_need(pos)
        soft_arm_need = max(self.min_lock_pct, fee_need if self.net_exits else 0.0)
        be_arm_need = max(self.min_lock_pct, be_buf)
        if pnl >= soft_arm_need:
            pos["armed"] = True
        be_ready = (not in_grace) and pnl >= be_arm_need
        pos["be_need_pct"] = be_arm_need
        pos["be_locked"] = False
        fee_covered = pnl >= fee_need

        # Refresh TP/SL from book; when fee-covered, snap TP to nearest opposite OB
        if self.exits == "wall":
            tp, sl, note = self._wall_tp_sl(
                side, entry, mid, bid_walls, ask_walls, best_bid, best_ask
            )
            ob_w = None
            if self.ob_tp_exit and fee_covered:
                ob_w = self._nearest_reward_ob(
                    side, entry, mid, bid_walls, ask_walls
                )
                if ob_w is not None:
                    # Pull TP to opposite OB (closer lock-in once fees are paid)
                    if side == "long":
                        tp = min(tp, float(ob_w["price"]))
                        if tp <= entry:
                            tp = float(ob_w["price"])
                    else:
                        tp = max(tp, float(ob_w["price"]))
                        if tp >= entry:
                            tp = float(ob_w["price"])
                    note = "ob-tp"
            pos["tp"] = tp
            dca_n = int(pos.get("dca_count") or 0)
            pos["exits"] = (f"dca{dca_n}+" if dca_n else "") + note
            if not in_grace:
                old_sl = float(pos["sl"])
                if side == "long":
                    floor_sl = entry * (1 - min_sl / 100)
                    cand = max(old_sl, sl)
                    if not pos.get("armed"):
                        cand = min(cand, floor_sl)
                    pos["sl"] = cand
                else:
                    ceil_sl = entry * (1 + min_sl / 100)
                    cand = min(old_sl, sl)
                    if not pos.get("armed"):
                        cand = max(cand, ceil_sl)
                    pos["sl"] = cand

        # BE locks *beyond* entry by be_buffer (short: above, long: below)
        protect_note = []
        if self.protect_be and be_ready:
            if side == "long":
                be_sl = entry * (1 - be_buf / 100)
                if float(pos["sl"]) < be_sl:
                    pos["sl"] = be_sl
                protect_note.append("BE")
                pos["be_locked"] = True
            else:
                be_sl = entry * (1 + be_buf / 100)
                if float(pos["sl"]) > be_sl:
                    pos["sl"] = be_sl
                protect_note.append("BE")
                pos["be_locked"] = True
        if self.protect_trail and pos.get("armed") and not in_grace:
            peak_mid = float(pos["peak_mid"])
            if side == "long":
                trail_sl = peak_mid * (1 - self.rev_pct / 100)
                be_floor = entry * (1 - be_buf / 100)
                if trail_sl > be_floor:
                    pos["sl"] = max(float(pos["sl"]), trail_sl)
                    protect_note.append("trail")
            else:
                trail_sl = peak_mid * (1 + self.rev_pct / 100)
                be_ceil = entry * (1 + be_buf / 100)
                if trail_sl < be_ceil:
                    pos["sl"] = min(float(pos["sl"]), trail_sl)
                    protect_note.append("trail")
        if protect_note:
            base = str(pos.get("exits") or "wall")
            pos["exits"] = base + "+" + "+".join(protect_note)

        soft_ok = held >= self.min_hold_sec
        soft_need = fee_need + max(0.0, self.min_edge_pct) * 0.5
        can_soft = soft_ok and (
            not self.net_exits or pnl >= soft_need
        )

        # Fee-covered @ opposite OB: touch / through wall, OR book/imb turns against
        if (
            self.ob_tp_exit
            and fee_covered
            and soft_ok
            and not in_grace
            and mid > 0
        ):
            ob_w = self._nearest_reward_ob(
                side, entry, mid, bid_walls, ask_walls
            )
            if ob_w is not None:
                ob_px = float(ob_w["price"])
                dist = abs(ob_px - mid) / mid * 100
                zone = max(self.touch_pct * 2.5, 0.15)
                near_ob = dist <= zone
                # Price reached/through the wall in profit direction
                through = (
                    (side == "long" and mid >= ob_px)
                    or (side == "short" and mid <= ob_px)
                )
                touch = dist <= self.touch_pct
                # Bid/ask (imbalance) turns against the position near the OB
                imb_against = (
                    (side == "long" and imb < 0.48)
                    or (side == "short" and imb > 0.52)
                )
                bp = float(self._book.get("pressure") or 0.5)
                btr = str(self._book.get("trend") or "")
                book_against = (
                    (side == "long" and (bp <= 0.45 or btr == "building-ask"))
                    or (side == "short" and (bp >= 0.55 or btr == "building-bid"))
                )
                if through or touch:
                    self._close_paper(mid, now, "ob")
                    return
                if near_ob and (imb_against or book_against):
                    self._close_paper(mid, now, "ob-rev")
                    return

        # Proactive: lock green when sense flips or price reverses from peak
        if pos.get("armed") and pnl > 0 and can_soft and not in_grace:
            if self.flip_exit:
                if side == "long" and imb < 0.5:
                    self._close_paper(mid, now, "flip")
                    return
                if side == "short" and imb > 0.5:
                    self._close_paper(mid, now, "flip")
                    return
            if self.rev_exit:
                peak_mid = float(pos["peak_mid"])
                if side == "long" and peak_mid > 0:
                    dd = (peak_mid - mid) / peak_mid * 100
                    if dd >= self.rev_pct:
                        self._close_paper(mid, now, "rev")
                        return
                if side == "short" and peak_mid > 0:
                    dd = (mid - peak_mid) / peak_mid * 100
                    if dd >= self.rev_pct:
                        self._close_paper(mid, now, "rev")
                        return

        # Giveback only after a clear break through entry (+ buffer), not a touch
        if self.giveback_exit and pos.get("armed") and soft_ok and not in_grace:
            if side == "long" and mid < entry * (1 - be_buf / 100) and pnl < 0:
                self._close_paper(mid, now, "give")
                return
            if side == "short" and mid > entry * (1 + be_buf / 100) and pnl < 0:
                self._close_paper(mid, now, "give")
                return

        # Hard exits — during grace, ignore SL unless adverse move is clearly wrong
        emergency = min_sl * 1.75
        if side == "long":
            if mid >= pos["tp"]:
                self._close_paper(mid, now, "tp")
            elif self.sl_enabled and mid <= pos["sl"]:
                adverse = (entry - mid) / entry * 100 if entry else 0.0
                if (not in_grace) or adverse >= emergency:
                    self._close_paper(mid, now, "sl")
        else:
            if mid <= pos["tp"]:
                self._close_paper(mid, now, "tp")
            elif self.sl_enabled and mid >= pos["sl"]:
                adverse = (mid - entry) / entry * 100 if entry else 0.0
                if (not in_grace) or adverse >= emergency:
                    self._close_paper(mid, now, "sl")

    def _nearest_wall(
        self, walls: list[dict[str, float]], *, below: bool, mid: float
    ) -> dict[str, float] | None:
        cands = []
        for w in walls:
            if w["notional"] < self.min_wall_usdt:
                continue
            if below and w["price"] < mid:
                cands.append(w)
            if not below and w["price"] > mid:
                cands.append(w)
        if not cands:
            return None
        return min(cands, key=lambda w: abs(w["dist_pct"]))

    def _levels_as_walls(
        self, levels: list[list[float]], mid: float
    ) -> list[dict[str, float]]:
        """Full depth → wall dicts (for entries; not just top-N by size)."""
        out: list[dict[str, float]] = []
        for price, qty in levels:
            out.append(
                {
                    "price": price,
                    "qty": qty,
                    "notional": price * qty,
                    "dist_pct": (price - mid) / mid * 100 if mid else 0.0,
                }
            )
        return out

    def _filter_chip(
        self,
        *,
        fid: str,
        label: str,
        value: str,
        enabled: bool,
        state: str,
        status: str,
        toggle: bool = True,
    ) -> dict[str, Any]:
        return {
            "id": fid,
            "label": label,
            "value": value,
            "enabled": enabled,
            "state": state,  # ok | block | wait | off
            "status": status,
            "toggle": toggle,
        }

    def _filter_bar_idle(self, why: str) -> list[dict[str, Any]]:
        flags = self.filter_flags()
        return [
            self._filter_chip(
                fid="wall", label="Wall", value="—", enabled=True,
                state="wait", status=why, toggle=False,
            ),
            self._filter_chip(
                fid="imb", label="Imbalance", value="—", enabled=True,
                state="wait", status=why, toggle=False,
            ),
            self._filter_chip(
                fid="bb", label="Bollinger", value=str(self._bb.get("label") or "—"),
                enabled=flags["bb"],
                state="off" if not flags["bb"] else ("ok" if self._bb.get("tradeable") else "block"),
                status="regime filter",
            ),
            self._filter_chip(
                fid="ema", label="EMA trend", value=str(self._trend.get("label") or "—"),
                enabled=flags["ema"],
                state="off" if not flags["ema"] else "wait",
                status="trend filter",
            ),
            self._filter_chip(
                fid="book", label="Book", value=str(self._book.get("label") or "—"),
                enabled=flags["book"],
                state="off" if not flags["book"] else "wait",
                status=str(self._book.get("trend") or ""),
            ),
            self._filter_chip(
                fid="mom", label="Momentum", value="—",
                enabled=flags["mom"],
                state="off" if not flags["mom"] else "wait",
                status="micro mom",
            ),
            self._filter_chip(
                fid="bounce", label="Bounce", value="off" if not flags["bounce"] else "on",
                enabled=flags["bounce"],
                state="off" if not flags["bounce"] else "wait",
                status="need bounce/reject",
            ),
            self._filter_chip(
                fid="ratio", label="Ratio", value="—",
                enabled=flags["ratio"],
                state="off" if not flags["ratio"] else "wait",
                status=f"min {self.min_wall_ratio:g}×",
            ),
            self._conf_filter_chip(),
            self._rsi_filter_chip(),
            self._adx_filter_chip(),
            self._smc_filter_chip(),
            self._dca_filter_chip(),
            self._obtp_filter_chip(),
        ]

    def _build_filter_bar(
        self,
        *,
        cand_side: str,
        imb: float,
        mom: float,
        near_bid: bool,
        near_ask: bool,
        bid_w: dict[str, float] | None,
        ask_w: dict[str, float] | None,
        conf: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from ob_ema import ema_allows

        flags = self.filter_flags()
        wall = bid_w if cand_side == "long" else ask_w
        near = near_bid if cand_side == "long" else near_ask
        if near_bid and near_ask:
            wall_val = "bid+ask"
        elif near_bid:
            wall_val = "bid"
        elif near_ask:
            wall_val = "ask"
        else:
            wall_val = "none"
        # ok = ready, wait = not yet, block = actively failing a ready setup
        wall_state = "ok" if near else "wait"
        if wall and not near:
            wall_st = f"{abs(float(wall['dist_pct'])):.3f}% away"
        elif wall and near:
            wall_st = (
                f"{abs(float(wall['dist_pct'])):.3f}% · "
                f"{float(wall['notional'])/1000:.0f}k"
            )
        else:
            wall_st = f"need ≤{self.touch_pct:g}%"

        if cand_side == "long":
            imb_ok = imb >= self.imb_long
            imb_st = f"≥{self.imb_long*100:.0f}%"
        else:
            imb_ok = imb <= self.imb_short
            imb_st = f"≤{self.imb_short*100:.0f}%"
        imb_state = "ok" if imb_ok else ("block" if near else "wait")

        bb_lab = str(self._bb.get("label") or "—")
        bb_ok = bool(self._bb.get("tradeable", True))
        if flags["bb"] and flags["bb_strict"] and bb_lab == "near-bb":
            bb_ok = False
        bb_state = "off" if not flags["bb"] else ("ok" if bb_ok else "block")

        ema_lab = str(self._trend.get("label") or "—")
        ema_ok = ema_allows(cand_side, self._ema_snap) if self._ema_snap is not None else True
        ema_state = "off" if not flags["ema"] else ("ok" if ema_ok else "block")

        bp = float(self._book.get("pressure") or 0.5)
        btr = str(self._book.get("trend") or "")
        book_ok = True
        if cand_side == "long" and (bp <= 0.38 or btr == "building-ask"):
            book_ok = False
        if cand_side == "short" and (bp >= 0.62 or btr == "building-bid"):
            book_ok = False
        book_state = "off" if not flags["book"] else ("ok" if book_ok else "block")

        mom_ok = True
        if cand_side == "long" and mom < -self.mom_max_against:
            mom_ok = False
        if cand_side == "short" and mom > self.mom_max_against:
            mom_ok = False
        mom_state = "off" if not flags["mom"] else ("ok" if mom_ok else "block")

        bounce_ok = True
        if flags["bounce"]:
            if cand_side == "long" and mom < 0.0:
                bounce_ok = False
            if cand_side == "short" and mom > 0.0:
                bounce_ok = False
        bounce_state = "off" if not flags["bounce"] else ("ok" if bounce_ok else "wait")

        wall_n = float((wall or {}).get("notional") or 0)
        opp = ask_w if cand_side == "long" else bid_w
        opp_n = float((opp or {}).get("notional") or 0)
        ratio = wall_n / opp_n if opp_n > 0 else 99.0
        ratio_ok = (not flags["ratio"]) or self.min_wall_ratio <= 0 or opp_n <= 0 or ratio >= self.min_wall_ratio
        ratio_state = "off" if not flags["ratio"] else ("ok" if ratio_ok else "block")

        return [
            self._filter_chip(
                fid="wall", label="Wall", value=wall_val, enabled=True,
                state=wall_state, status=wall_st, toggle=False,
            ),
            self._filter_chip(
                fid="imb", label="Imbalance", value=f"{imb*100:.1f}%", enabled=True,
                state=imb_state, status=f"{cand_side} {imb_st}", toggle=False,
            ),
            self._filter_chip(
                fid="bb", label="Bollinger", value=bb_lab, enabled=flags["bb"],
                state=bb_state, status="strict" if flags["bb_strict"] else "regime",
            ),
            self._filter_chip(
                fid="ema", label="EMA trend", value=ema_lab, enabled=flags["ema"],
                state=ema_state, status=f"allow {cand_side}" if ema_ok else f"blocks {cand_side}",
            ),
            self._filter_chip(
                fid="book", label="Book",
                value=f"{str(self._book.get('label') or '—')[:9]} {bp*100:.0f}%",
                enabled=flags["book"],
                state=book_state, status=btr or "ladder",
            ),
            self._filter_chip(
                fid="mom", label="Momentum", value=f"{mom:+.3f}%",
                enabled=flags["mom"],
                state=mom_state, status=f"max against {self.mom_max_against:g}%",
            ),
            self._filter_chip(
                fid="bounce", label="Bounce",
                value=("up" if mom > 0 else ("dn" if mom < 0 else "flat")),
                enabled=flags["bounce"],
                state=bounce_state, status="off=ignore" if not flags["bounce"] else "need align",
            ),
            self._filter_chip(
                fid="ratio", label="Ratio",
                value=("∞" if opp_n <= 0 else f"{ratio:.2f}×"),
                enabled=flags["ratio"],
                state=ratio_state, status=f"min {self.min_wall_ratio:g}×",
            ),
            self._conf_filter_chip(conf),
            self._rsi_filter_chip(cand_side),
            self._adx_filter_chip(cand_side),
            self._smc_filter_chip(cand_side),
            self._dca_filter_chip(),
            self._obtp_filter_chip(),
        ]

    def _rsi_filter_chip(self, cand_side: str = "long") -> dict[str, Any]:
        flags = self.filter_flags()
        rsi = self._osc.get("rsi")
        val = "—" if rsi is None else f"{float(rsi):.1f}"
        if not flags["rsi"]:
            return self._filter_chip(
                fid="rsi", label="RSI", value=val, enabled=False,
                state="off", status="disabled",
            )
        ok = self._rsi_allows(cand_side)
        side_lab = str(self._osc.get("rsi_side") or "none")
        status = (
            f"{cand_side} ok · {side_lab}"
            if ok
            else f"blocks {cand_side} (OS/OB)"
        )
        return self._filter_chip(
            fid="rsi", label="RSI", value=val, enabled=True,
            state="ok" if ok else "block",
            status=status,
        )

    def _adx_filter_chip(self, cand_side: str = "long") -> dict[str, Any]:
        flags = self.filter_flags()
        adx = self._osc.get("adx")
        di_p = self._osc.get("di_plus")
        di_m = self._osc.get("di_minus")
        if adx is None:
            val = "—"
        else:
            val = f"{float(adx):.0f}"
            if di_p is not None and di_m is not None:
                val = f"{float(adx):.0f} ±{float(di_p):.0f}/{float(di_m):.0f}"
        if not flags["adx"]:
            return self._filter_chip(
                fid="adx", label="ADX", value=val, enabled=False,
                state="off", status="disabled",
            )
        ok = self._adx_allows(cand_side)
        if adx is not None and float(adx) < self.adx_min:
            status = f"need ≥{self.adx_min:g}"
            state = "wait"
        elif ok:
            status = f"trend {self._osc.get('adx_side')}"
            state = "ok"
        else:
            status = f"DI against {cand_side}"
            state = "block"
        return self._filter_chip(
            fid="adx", label="ADX", value=val, enabled=True,
            state=state, status=status,
        )

    def _smc_filter_chip(self, cand_side: str = "long") -> dict[str, Any]:
        flags = self.filter_flags()
        st = self._structure or {}
        choch = str(st.get("choch") or "none")
        bits = []
        if choch != "none":
            bits.append(choch)
        if st.get("eql"):
            bits.append("EQL")
        if st.get("eqh"):
            bits.append("EQH")
        val = " · ".join(bits) if bits else str(st.get("label") or "flat")
        if not flags["smc"]:
            return self._filter_chip(
                fid="smc", label="SMC", value=val, enabled=False,
                state="off", status="display only (header)",
            )
        ok = self._smc_allows(cand_side)
        status = (
            f"allow {cand_side}"
            if ok
            else f"structure against {cand_side}"
        )
        return self._filter_chip(
            fid="smc", label="SMC", value=val, enabled=True,
            state="ok" if ok else "block",
            status=status,
        )

    def _conf_filter_chip(self, conf: dict[str, Any] | None = None) -> dict[str, Any]:
        flags = self.filter_flags()
        c = conf if conf is not None else (self._confidence or {})
        score = float(c.get("score") or 0)
        val = f"{score:.0f}/{self.min_confidence:.0f}"
        if not flags["conf"]:
            return self._filter_chip(
                fid="conf", label="Confidence", value=val, enabled=False,
                state="off", status="disabled",
            )
        if score >= self.min_confidence:
            return self._filter_chip(
                fid="conf", label="Confidence", value=val, enabled=True,
                state="ok", status="setup score",
            )
        if score <= 0:
            return self._filter_chip(
                fid="conf", label="Confidence", value=val, enabled=True,
                state="wait", status="no setup yet",
            )
        return self._filter_chip(
            fid="conf", label="Confidence", value=val, enabled=True,
            state="block", status=f"need ≥{self.min_confidence:.0f}",
        )

    def _dca_filter_chip(self) -> dict[str, Any]:
        flags = self.filter_flags()
        pos = self._paper
        n = int((pos or {}).get("dca_count") or 0)
        val = f"{n}/{self.dca_max}"
        if not flags["dca"]:
            state = "off"
            status = "disabled"
        elif pos and float(pos.get("pnl_pct") or 0) < -self.dca_min_loss_pct:
            if n >= self.dca_max:
                state = "block"
                status = "max DCA"
            else:
                state = "wait"
                status = "armed @ next OB"
        elif pos:
            state = "ok"
            status = "only in loss"
        else:
            state = "wait"
            status = f"loss≥{self.dca_min_loss_pct:g}% @ OB"
        return self._filter_chip(
            fid="dca", label="DCA", value=val,
            enabled=flags["dca"], state=state, status=status,
        )

    def _obtp_filter_chip(self) -> dict[str, Any]:
        flags = self.filter_flags()
        pos = self._paper
        if not flags["obtp"]:
            return self._filter_chip(
                fid="obtp", label="OB take-profit", value="off", enabled=False,
                state="off", status="disabled",
            )
        if not pos:
            return self._filter_chip(
                fid="obtp", label="OB take-profit", value="on", enabled=True,
                state="wait", status="touch/through OB or bid/ask against",
            )
        fee_need = self._fee_cover_need(pos)
        pnl = float(pos.get("pnl_pct") or 0)
        if pnl >= fee_need:
            return self._filter_chip(
                fid="obtp", label="OB take-profit", value="armed", enabled=True,
                state="ok", status="fees ok · exit on OB/reject",
            )
        return self._filter_chip(
            fid="obtp", label="OB take-profit", value=f"+{max(0, fee_need - pnl):.3f}%",
            enabled=True, state="wait", status="need fee cover",
        )

    def _eval_signal(
        self,
        mid: float,
        imb: float,
        bid_walls: list[dict[str, float]],
        ask_walls: list[dict[str, float]],
        now: float,
        *,
        all_bids: list[dict[str, float]] | None = None,
        all_asks: list[dict[str, float]] | None = None,
        spread: float = 0.0,
    ) -> str:
        from ob_ema import ema_allows

        bids = all_bids if all_bids is not None else bid_walls
        asks = all_asks if all_asks is not None else ask_walls

        # Always compute live chips (even in position / cooldown) so they are not stuck amber
        self._bb = self._bb_regime_for_mid(mid)
        bid_w = self._nearest_wall(bids, below=True, mid=mid)
        ask_w = self._nearest_wall(asks, below=False, mid=mid)
        need = self._min_tp_dist_pct()
        long_tp = self._reward_wall(asks, above=True, mid=mid, min_dist_pct=need)
        short_tp = self._reward_wall(bids, above=False, mid=mid, min_dist_pct=need)
        best_bid = max((w["price"] for w in bids), default=None)
        best_ask = min((w["price"] for w in asks), default=None)
        near_bid = bool(bid_w and abs(bid_w["dist_pct"]) <= self.touch_pct)
        near_ask = bool(ask_w and abs(ask_w["dist_pct"]) <= self.touch_pct)
        mom = self._recent_mom_pct()

        def _conf(side: str, wall: dict[str, float] | None) -> dict[str, Any]:
            return self._score_confidence(
                side,
                mid=mid,
                imb=imb,
                entry_wall=wall or {"notional": 0},
                opp_wall=ask_w if side == "long" else bid_w,
                tp_wall=long_tp if side == "long" else short_tp,
                spread=spread,
                mom_pct=mom,
            )

        if self._paper:
            cand_side = str(self._paper["side"])
            cand_wall = bid_w if cand_side == "long" else ask_w
        elif near_bid or (imb >= self.imb_long and bid_w):
            cand_side, cand_wall = "long", bid_w
        elif near_ask or (imb <= self.imb_short and ask_w):
            cand_side, cand_wall = "short", ask_w
        else:
            cand_side = "long" if imb >= 0.5 else "short"
            cand_wall = bid_w if cand_side == "long" else ask_w
        self._confidence = _conf(cand_side, cand_wall)
        self._filters = self._build_filter_bar(
            cand_side=cand_side,
            imb=imb,
            mom=mom,
            near_bid=near_bid,
            near_ask=near_ask,
            bid_w=bid_w,
            ask_w=ask_w,
            conf=self._confidence,
        )

        if not self.paper_enabled:
            self._block_reason = "paper off"
            return "flat"
        if self._paper:
            self._block_reason = "in position"
            return self._paper["side"]
        if now < self._cooldown_until:
            left = max(0.0, self._cooldown_until - now)
            self._block_reason = f"cooldown {left:.1f}s left"
            return "flat"

        if self.bb_filter and not self._bb.get("tradeable", True):
            self._block_reason = f"regime {self._bb.get('label', '?')} (need inside/near BB)"
            return "flat"

        def _try_open(side: str, wall: dict[str, float], why: str) -> bool:
            conf = _conf(side, wall)
            self._confidence = conf
            self._filters = self._build_filter_bar(
                cand_side=side,
                imb=imb,
                mom=mom,
                near_bid=bool(near_bid),
                near_ask=bool(near_ask),
                bid_w=bid_w,
                ask_w=ask_w,
                conf=conf,
            )
            if self.bb_filter and self.bb_strict and str(self._bb.get("label")) == "near-bb":
                self._block_reason = "bb near-edge (strict)"
                return False
            if self.ema_filter and not ema_allows(side, self._ema_snap):
                trend = (self._trend or {}).get("label", "?")
                self._block_reason = f"ema {trend} blocks {side}"
                return False
            if self.book_filter:
                bp = float(self._book.get("pressure") or 0.5)
                btr = str(self._book.get("trend") or "")
                if side == "long" and (bp <= 0.38 or btr == "building-ask"):
                    self._block_reason = (
                        f"book {self._book.get('label')} "
                        f"({bp*100:.0f}% bid) against long"
                    )
                    return False
                if side == "short" and (bp >= 0.62 or btr == "building-bid"):
                    self._block_reason = (
                        f"book {self._book.get('label')} "
                        f"({bp*100:.0f}% bid) against short"
                    )
                    return False
            if self.mom_filter:
                if side == "long" and mom < -self.mom_max_against:
                    self._block_reason = f"mom {mom:+.3f}% still dumping"
                    return False
                if side == "short" and mom > self.mom_max_against:
                    self._block_reason = f"mom {mom:+.3f}% still pumping"
                    return False
            if self.require_bounce:
                if side == "long" and mom < 0.0:
                    self._block_reason = "wait bounce off bid wall"
                    return False
                if side == "short" and mom > 0.0:
                    self._block_reason = "wait rejection off ask wall"
                    return False
            wall_n = float(wall.get("notional") or 0)
            opp = ask_w if side == "long" else bid_w
            opp_n = float((opp or {}).get("notional") or 0)
            ratio = wall_n / opp_n if opp_n > 0 else 99.0
            if self.ratio_filter and self.min_wall_ratio > 0 and opp_n > 0 and ratio < self.min_wall_ratio:
                self._block_reason = (
                    f"wall ratio {ratio:.2f} < {self.min_wall_ratio:g}"
                )
                return False
            if self.conf_filter and conf["score"] < self.min_confidence:
                self._block_reason = (
                    f"conf {conf['score']:.0f} < min {self.min_confidence:.0f}"
                )
                return False
            if self.rsi_filter and not self._rsi_allows(side):
                rsi = self._osc.get("rsi")
                self._block_reason = f"rsi {float(rsi or 0):.1f} blocks {side}"
                return False
            if self.adx_filter and not self._adx_allows(side):
                adx = self._osc.get("adx")
                bias = self._osc.get("adx_side") or "none"
                self._block_reason = (
                    f"adx {float(adx or 0):.0f}/{bias} blocks {side}"
                )
                return False
            if self.smc_filter and not self._smc_allows(side):
                lab = (self._structure or {}).get("label") or "?"
                self._block_reason = f"smc {lab} blocks {side}"
                return False
            self._open_paper(
                side, mid, wall["price"], now, why,
                bid_walls=bids, ask_walls=asks,
                best_bid=best_bid, best_ask=best_ask,
            )
            self._block_reason = ""
            return True

        if near_bid and imb >= self.imb_long:
            if _try_open("long", bid_w, "bid-wall+imb"):
                return "long"
            if self._block_reason:
                return "flat"
        if near_ask and imb <= self.imb_short:
            if _try_open("short", ask_w, "ask-wall+imb"):
                return "short"
            if self._block_reason:
                return "flat"

        reasons = []
        if not near_bid and not near_ask:
            reasons.append(f"no wall≥{self.min_wall_usdt/1000:.0f}k within {self.touch_pct:g}%")
        elif near_bid and imb < self.imb_long:
            reasons.append(f"imb {imb*100:.1f}% < long {self.imb_long*100:.0f}%")
        elif near_ask and imb > self.imb_short:
            reasons.append(f"imb {imb*100:.1f}% > short {self.imb_short*100:.0f}%")
        if near_bid and long_tp is None:
            reasons.append(f"TP via pct (no ask wall ≥{need:g}%)")
        if near_ask and short_tp is None:
            reasons.append(f"TP via pct (no bid wall ≥{need:g}%)")
        if self.ema_filter and self._ema_snap is not None:
            if near_bid and imb >= self.imb_long and not self._ema_snap.allow_long:
                reasons.append(f"ema {self._trend.get('label')}≠bull")
            if near_ask and imb <= self.imb_short and not self._ema_snap.allow_short:
                reasons.append(f"ema {self._trend.get('label')}≠bear")
        score = float(self._confidence.get("score") or 0)
        if self.conf_filter and score < self.min_confidence and (near_bid or near_ask):
            reasons.append(f"conf {score:.0f}<{self.min_confidence:.0f}")
        if not reasons:
            reasons.append("filters not met")
        self._block_reason = " · ".join(reasons)
        return "flat"

    def _fetch_book(self) -> dict[str, Any]:
        url = f"{FAPI_BASE}/fapi/v1/depth?symbol={self.symbol}&limit={self.limit}"
        raw = _http_get_json(url)
        bids = [[float(p), float(q)] for p, q in raw.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in raw.get("asks", [])]
        if not bids or not asks:
            raise RuntimeError("empty book")
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
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
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_walls": self._top_walls(bids, mid, self.walls),
            "ask_walls": self._top_walls(asks, mid, self.walls),
            "all_bids": self._levels_as_walls(bids, mid),
            "all_asks": self._levels_as_walls(asks, mid),
            "depth_profile": self._depth_profile(bids, asks, mid, levels=min(28, self.limit)),
            "book": self._update_book_pressure(
                bids, asks, mid, levels=min(28, self.limit)
            ),
            "ts": now,
            "ts_iso": time.strftime("%H:%M:%S", time.localtime(now)),
            "error": None,
        }

    def loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                snap = self._fetch_book()
                mid = float(snap["mid"])
                now = float(snap["ts"])
                self._refresh_fees(now)
                self._refresh_bb(now, mid)
                self._refresh_ema(now)
                self._refresh_osc(now)
                self._refresh_structure(now)
                self._refresh_live(now)
                imb = float(snap["imbalance"])
                self._manage_paper(
                    mid,
                    now,
                    imb,
                    bid_walls=snap["all_bids"],
                    ask_walls=snap["all_asks"],
                    best_bid=snap.get("best_bid"),
                    best_ask=snap.get("best_ask"),
                )
                self._signal = self._eval_signal(
                    mid,
                    imb,
                    snap["bid_walls"],
                    snap["ask_walls"],
                    now,
                    all_bids=snap["all_bids"],
                    all_asks=snap["all_asks"],
                    spread=float(snap.get("spread") or 0),
                )
                unreal = 0.0
                if self._paper:
                    unreal = self._pnl_pct(
                        self._paper["entry"], mid, self._paper["side"]
                    )
                    self._paper["pnl_pct"] = unreal
                    # Show open pnl net of half fee already paid conceptually (entry leg)
                    self._paper["net_pct"] = unreal - self.fee_rt_pct / 2

                with self._lock:
                    t_sec = int(now)
                    if self._trail and self._trail[-1]["t"] == t_sec:
                        self._trail[-1] = {"t": t_sec, "mid": mid}
                    else:
                        self._trail.append({"t": t_sec, "mid": mid})
                    cutoff = now - self.trail_sec
                    while self._trail and self._trail[0]["t"] < cutoff:
                        self._trail.popleft()
                    # Drop markers outside trail window (Lightweight Charts requires time in data)
                    t_min = self._trail[0]["t"] if self._trail else 0
                    self._markers = [m for m in self._markers if m["t"] >= t_min]
                    pub = {
                        k: v
                        for k, v in snap.items()
                        if k not in ("all_bids", "all_asks")
                    }
                    self._snapshot = {
                        "symbol": self.symbol,
                        **pub,
                        "trail": list(self._trail),
                        "bb_trail": list(self._bb_trail),
                        "regime": dict(self._bb),
                        "signal": self._signal,
                        "block_reason": self._block_reason,
                        "dry_run": self.dry_run,
                        "sl_enabled": self.sl_enabled,
                        "sl_hits": self.sl_hits,
                        "sl_to_dry": self.sl_to_dry,
                        "order_error": self._last_order_error,
                        "trend": dict(self._trend),
                        "structure": dict(self._structure),
                        "osc": dict(self._osc),
                        "book": dict(self._book),
                        "confidence": dict(self._confidence),
                        "filters": list(self._filters),
                        "paper": dict(self._paper) if self._paper else {},
                        "live": dict(self._live),
                        "live_enabled": self.live_enabled,
                        "markers": list(self._markers),
                        "trades": list(self._trades),
                        "session": self._session_view(unreal),
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

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
                self._json(200, state.snapshot())
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/close":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 0:
                        self.rfile.read(n)
                    result = state.manual_close()
                    self._json(200 if result.get("ok") else 400, result)
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})
                return
            if path == "/api/live":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n > 0 else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                    if "enabled" not in data:
                        self._json(400, {"ok": False, "error": "missing enabled"})
                        return
                    result = state.set_live(bool(data.get("enabled")))
                    self._json(200 if result.get("ok") else 400, result)
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})
                return
            if path == "/api/sl":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n > 0 else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                    if "enabled" not in data:
                        self._json(400, {"ok": False, "error": "missing enabled"})
                        return
                    result = state.set_sl(bool(data.get("enabled")))
                    self._json(200 if result.get("ok") else 400, result)
                except (ValueError, json.JSONDecodeError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})
                return
            if path != "/api/filter":
                self.send_response(404)
                self.end_headers()
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
                fid = str(data.get("id") or "")
                enabled = bool(data.get("enabled"))
                flags = state.set_filter(fid, enabled)
                self._json(200, {"ok": True, "id": fid, "enabled": enabled, "flags": flags})
            except KeyError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description="Live OB wall chart + scalper (LIVE by default)")
    p.add_argument("symbol", nargs="?", default="BTCUSDT")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--limit", type=int, default=100, help="depth levels")
    p.add_argument("--walls", type=int, default=8, help="top walls per side")
    p.add_argument("--band", type=float, default=0.15, help="imbalance band %% around mid")
    p.add_argument("--sample-sec", type=float, default=0.5, help="depth poll interval")
    p.add_argument("--trail-sec", type=int, default=900, help="price trail length (sec)")
    p.add_argument("--ui-ms", type=int, default=400, help="browser poll interval")
    p.add_argument("--exits", choices=("wall", "pct"), default="wall",
                   help="TP/SL mode: wall=dynamic bid/ask walls (default), pct=fixed %%")
    p.add_argument("--tp-pct", type=float, default=0.08, help="fallback / pct-mode TP %%")
    p.add_argument("--sl-pct", type=float, default=0.22, help="fallback / pct-mode SL %%")
    p.add_argument("--min-sl-pct", type=float, default=0.22,
                   help="min SL distance from entry %% (stops glued-to-mid noise exits)")
    p.add_argument("--max-tp-pct", type=float, default=0.25,
                   help="cap dynamic TP distance from entry %%")
    p.add_argument("--sl-buffer-pct", type=float, default=0.03,
                   help="place SL this %% beyond the support/resistance wall")
    p.add_argument("--sl-grace-sec", type=float, default=6.0,
                   help="after entry, ignore noise SL hits (unless adverse ≥1.75× min-sl)")
    p.add_argument("--be-buffer-pct", type=float, default=0.04,
                   help="BE lock places SL this %% beyond entry (survives entry retest)")
    p.add_argument("--touch-pct", type=float, default=0.10, help="max dist %% to wall for entry")
    p.add_argument("--min-wall-usdt", type=float, default=35_000.0, help="min wall notional")
    p.add_argument("--min-wall-ratio", type=float, default=1.0,
                   help="entry wall must be this × opposite nearest wall (0=off)")
    p.add_argument("--imb-long", type=float, default=0.54, help="min bid imbalance for long")
    p.add_argument("--imb-short", type=float, default=0.46, help="max bid imbalance for short")
    p.add_argument("--bb-period", type=int, default=20)
    p.add_argument("--bb-std", type=float, default=2.0)
    p.add_argument("--bb-interval", default="5m",
                   help="BB regime filter timeframe (not the entry signal)")
    p.add_argument("--bb-pad-pct", type=float, default=0.25,
                   help="allow entries this %% outside BB bands")
    p.add_argument("--bb-strict", action=argparse.BooleanOptionalAction, default=False,
                   help="only enter inside BB (reject near-bb edge)")
    p.add_argument("--cooldown-sec", type=float, default=6.0,
                   help="pause after close (SL forces at least 12s)")
    p.add_argument("--min-edge-pct", type=float, default=0.03,
                   help="extra %% beyond fees preferred for opposite wall TP")
    p.add_argument("--min-hold-sec", type=float, default=3.0,
                   help="min seconds in trade before soft exits (flip/rev/give)")
    p.add_argument("--require-bounce", action=argparse.BooleanOptionalAction, default=True,
                   help="enter only after micro-bounce/rejection off the wall")
    p.add_argument("--mom-max-against", type=float, default=0.06,
                   help="block entry if short-term mid mom %% is against side by more than this")
    p.add_argument("--book-filter", action=argparse.BooleanOptionalAction, default=True,
                   help="use depth-ladder pressure as micro-trend (block strong opposite stack)")
    p.add_argument("--dca", action=argparse.BooleanOptionalAction, default=True,
                   help="add size on adverse OB walls while in loss; rebase avg + TP/SL")
    p.add_argument("--dca-max", type=int, default=8, help="max DCA adds per position")
    p.add_argument("--dca-min-loss", type=float, default=0.05,
                   help="min unrealized loss %% before first DCA")
    p.add_argument("--dca-space-pct", type=float, default=0.10,
                   help="min %% between DCA walls (same OB zone = skipped)")
    p.add_argument("--dca-cooldown-sec", type=float, default=12.0,
                   help="min seconds between DCA adds")
    p.add_argument("--ob-tp", action=argparse.BooleanOptionalAction, default=True,
                   help="when fees covered, snap TP to opposite OB and close on touch")
    p.add_argument("--sl-to-dry", type=int, default=3,
                   help="after N session SL hits while LIVE → PAPER (0=never; default 3)")
    p.add_argument("--rsi-filter", action=argparse.BooleanOptionalAction, default=False,
                   help="block long in RSI overbought / short in oversold (default off)")
    p.add_argument("--adx-filter", action=argparse.BooleanOptionalAction, default=True,
                   help="require ADX≥min and DI aligned with side")
    p.add_argument("--adx-min", type=float, default=20.0,
                   help="min ADX for trend filter (default 20)")
    p.add_argument("--smc-filter", action=argparse.BooleanOptionalAction, default=False,
                   help="block entries against SMC structure bias (pill always shown)")
    p.add_argument("--osc-interval", default="5m",
                   help="RSI/ADX kline interval")
    p.add_argument("--structure-interval", default="5m",
                   help="SMC / iCHoCH kline interval")
    p.add_argument("--net-exits", action=argparse.BooleanOptionalAction, default=True,
                   help="only soft-exit when gross pnl covers fee estimate")
    p.add_argument("--protect-be", action=argparse.BooleanOptionalAction, default=True,
                   help="once slightly green (after grace), move SL to breakeven ± buffer")
    p.add_argument("--protect-trail", action=argparse.BooleanOptionalAction, default=True,
                   help="once armed, trail SL under/over peak by --rev-pct")
    p.add_argument("--ema-filter", action=argparse.BooleanOptionalAction, default=True,
                   help="hard filter: only long in bullish EMA / short in bearish")
    p.add_argument("--ema-interval", default="1m", help="EMA kline interval")
    p.add_argument("--ema-fast", type=int, default=7)
    p.add_argument("--ema-slow", type=int, default=25)
    p.add_argument("--ema-slope-min", type=float, default=0.05,
                   help="min |EMA fast slope| %% over ~5 bars to count as trend")
    p.add_argument("--min-confidence", type=float, default=35.0,
                   help="min 0–100 setup score (imb+wall+ratio+BB+EMA+mom+TP) to enter")
    p.add_argument("--min-lock-pct", type=float, default=0.01,
                   help="min paper profit %% before proactive exits arm")
    p.add_argument("--rev-pct", type=float, default=0.02,
                   help="close while green if pullback from peak >= this %%")
    p.add_argument("--flip-exit", action=argparse.BooleanOptionalAction, default=True,
                   help="close while green when imbalance flips against position")
    p.add_argument("--rev-exit", action=argparse.BooleanOptionalAction, default=True,
                   help="close while green on pullback from peak")
    p.add_argument("--giveback-exit", action=argparse.BooleanOptionalAction, default=True,
                   help="if was green then mid crosses entry, close immediately")
    p.add_argument("--fee-rt-pct", type=float, default=0.08,
                   help="fallback round-trip fee %% if Binance commissionRate unavailable")
    p.add_argument("--fee-mode", choices=("taker", "maker", "mixed"), default="taker",
                   help="how to build RT from Binance rates: 2*taker | 2*maker | maker+taker")
    p.add_argument("--notional", type=float, default=0.0,
                   help="order size in USDT; 0 (default) = exchange minimum for the symbol")
    p.add_argument("--dry-run", action="store_true",
                   help="paper only — no real orders (default is LIVE)")
    p.add_argument("--no-trade", action="store_true",
                   help="chart only — disable entries entirely")
    p.add_argument("--live-pos", action="store_true",
                   help="overlay exchange position (on by default when LIVE)")
    p.add_argument("--recv-window", type=int, default=15_000)
    args = p.parse_args()

    from orderbook_dca_grid import (
        _resolve_hedge,
        load_env_file,
        load_keys,
        load_symbol_filters,
    )

    load_env_file(None)
    api_key, api_secret = load_keys(None)
    dry_run = bool(args.dry_run)
    filt: dict[str, Decimal] | None = None
    hedge = False

    if not dry_run:
        if not api_key or not api_secret:
            print("LIVE mode needs API keys in .env — or pass --dry-run", file=sys.stderr)
            sys.exit(1)
        try:
            filt = load_symbol_filters(args.symbol)
        except Exception as exc:
            print(f"Could not load symbol filters: {exc}", file=sys.stderr)
            sys.exit(1)
        ns = argparse.Namespace(position_mode="auto", recv_window=args.recv_window)
        hedge = _resolve_hedge(ns, api_key, api_secret)
    elif args.live_pos and api_key and api_secret:
        ns = argparse.Namespace(position_mode="auto", recv_window=args.recv_window)
        hedge = _resolve_hedge(ns, api_key, api_secret)

    state = BookState(
        args.symbol,
        limit=args.limit,
        walls=args.walls,
        band_pct=args.band,
        trail_sec=args.trail_sec,
        sample_sec=args.sample_sec,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        min_sl_pct=args.min_sl_pct,
        touch_pct=args.touch_pct,
        min_wall_usdt=args.min_wall_usdt,
        imb_long=args.imb_long,
        imb_short=args.imb_short,
        bb_period=args.bb_period,
        bb_std=args.bb_std,
        bb_interval=args.bb_interval,
        bb_pad_pct=args.bb_pad_pct,
        cooldown_sec=args.cooldown_sec,
        paper=not args.no_trade,
        dry_run=dry_run,
        live_pos=args.live_pos or (not dry_run),
        api_key=api_key,
        api_secret=api_secret,
        recv_window=args.recv_window,
        hedge=hedge,
        filt=filt,
        flip_exit=args.flip_exit,
        rev_exit=args.rev_exit,
        min_lock_pct=args.min_lock_pct,
        rev_pct=args.rev_pct,
        giveback_exit=args.giveback_exit,
        exits=args.exits,
        sl_buffer_pct=args.sl_buffer_pct,
        max_tp_pct=args.max_tp_pct,
        fee_rt_pct=args.fee_rt_pct,
        notional=args.notional,
        min_edge_pct=args.min_edge_pct,
        min_hold_sec=args.min_hold_sec,
        net_exits=args.net_exits,
        fee_mode=args.fee_mode,
        protect_be=args.protect_be,
        protect_trail=args.protect_trail,
        be_buffer_pct=args.be_buffer_pct,
        sl_grace_sec=args.sl_grace_sec,
        ema_filter=args.ema_filter,
        ema_interval=args.ema_interval,
        ema_fast=args.ema_fast,
        ema_slow=args.ema_slow,
        ema_slope_min=args.ema_slope_min,
        min_confidence=args.min_confidence,
        min_wall_ratio=args.min_wall_ratio,
        mom_max_against=args.mom_max_against,
        require_bounce=args.require_bounce,
        bb_strict=args.bb_strict,
        book_filter=args.book_filter,
    )
    state.dca_enabled = bool(args.dca)
    state.dca_max = max(0, int(args.dca_max))
    state.dca_min_loss_pct = float(args.dca_min_loss)
    state.dca_space_pct = max(0.02, float(args.dca_space_pct))
    state.dca_cooldown_sec = max(0.0, float(args.dca_cooldown_sec))
    state.ob_tp_exit = bool(args.ob_tp)
    state.sl_to_dry = max(0, int(args.sl_to_dry))
    state.rsi_filter = bool(args.rsi_filter)
    state.adx_filter = bool(args.adx_filter)
    state.adx_min = float(args.adx_min)
    state.smc_filter = bool(args.smc_filter)
    state.osc_interval = str(args.osc_interval)
    state.structure_interval = str(args.structure_interval)
    # Pull Binance commission rates immediately (needs API keys in .env)
    state._refresh_fees(0.0)

    worker = threading.Thread(target=state.loop, name="depth", daemon=True)
    worker.start()

    handler = make_handler(state, args.ui_ms)
    try:
        httpd, port = bind_dashboard(args.host, args.port, handler)
    except RuntimeError as exc:
        print(f"Could not bind dashboard: {exc}", file=sys.stderr)
        state.stop()
        sys.exit(1)
    if port != args.port:
        print(f"port {args.port} busy → using {port}")
    url = f"http://{args.host}:{port}/"
    mode = "DRY-RUN" if dry_run else "LIVE"
    if args.no_trade:
        mode = "CHART-ONLY"
    print(f"OB live chart  {state.symbol}  [{mode}]  →  {url}")
    if not dry_run and not args.no_trade:
        print("*** LIVE TRADING — real MARKET orders on Binance Futures ***")
        if args.notional > 0:
            size_txt = f"≈ {args.notional:g} USDT"
        else:
            size_txt = "exchange minimum (minQty / minNotional)"
        print(f"*** size {size_txt} per entry  hedge={hedge} ***")
    print(
        f"exits={args.exits}  TP% {args.tp_pct:g}  "
        f"SL≥{max(args.sl_pct, args.min_sl_pct):g}%  "
        f"sl-buf {args.sl_buffer_pct:g}%  max-tp {args.max_tp_pct:g}%  "
        f"touch {args.touch_pct:g}%  min-wall {args.min_wall_usdt:,.0f} USDT"
    )
    print(
        f"proactive: flip={args.flip_exit} rev={args.rev_exit} "
        f"giveback={args.giveback_exit}  arm>={args.min_lock_pct:g}%  rev={args.rev_pct:g}%"
    )
    print(
        f"protect: BE={args.protect_be} trail={args.protect_trail} "
        f"be-buf={args.be_buffer_pct:g}%  sl-grace={args.sl_grace_sec:g}s"
    )
    fee_src = state._session.get("fee_source", "fallback")
    print(
        f"fees [{fee_src}] mode={args.fee_mode}  RT={state.fee_rt_pct:g}%  "
        f"maker={state._session.get('maker_pct', 0):g}%  "
        f"taker={state._session.get('taker_pct', 0):g}%  "
        f"notional {args.notional:g} USDT"
    )
    if fee_src != "binance":
        print("  (put BINANCE API keys in .env to load /fapi/v1/commissionRate)")
        if state._session.get("fee_error"):
            print(f"  fee error: {state._session['fee_error']}")
    need = state.fee_rt_pct + args.min_edge_pct
    print(
        f"fee recover: min TP wall {need:g}%  bb={args.bb_interval}  "
        f"net-exits={args.net_exits}  min-hold={args.min_hold_sec:g}s"
    )
    print(
        f"trend: ema-filter={args.ema_filter}  "
        f"{args.ema_fast}/{args.ema_slow} {args.ema_interval}  "
        f"slope≥{args.ema_slope_min:g}%  min-conf={args.min_confidence:g}"
    )
    print(
        f"entries: imb {args.imb_long:g}/{args.imb_short:g}  "
        f"wall≥{args.min_wall_usdt:,.0f}  ratio≥{args.min_wall_ratio:g}  "
        f"bounce={args.require_bounce}  bb-strict={args.bb_strict}  "
        f"book-filter={args.book_filter}  "
        f"mom-against≤{args.mom_max_against:g}%  "
        f"dca={args.dca} max={args.dca_max} loss≥{args.dca_min_loss:g}%"
    )
    if dry_run:
        print("Dry-run: no real orders. Ctrl+C to stop.")
    else:
        print("LIVE: entries/exits send MARKET orders. Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        state.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
