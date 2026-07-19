#!/usr/bin/env python3
"""Live order-book wall dashboard + OB scalper (Binance Futures).

Polls depth, draws mid + walls / depth profile, Bollinger regime, and trades
wall-bounce entries with dynamic TP/SL. By default places REAL market orders.
Pass --dry-run for paper-only simulation.

Usage:
    python3 ob_live_chart.py SOLUSDT                  # LIVE orders
    python3 ob_live_chart.py SOLUSDT --dry-run        # paper only
    python3 ob_live_chart.py BTCUSDT                 # LIVE, exchange min size
    python3 ob_live_chart.py BTCUSDT --notional 50   # LIVE, ~50 USDT
    # open http://127.0.0.1:8765/
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
      grid-template-columns: 1fr 168px 280px;
      gap: 0;
      min-height: calc(100vh - 70px);
    }
    @media (max-width: 1100px) {
      .wrap { grid-template-columns: 1fr 150px; }
      aside { display: none; }
    }
    @media (max-width: 720px) {
      .wrap { grid-template-columns: 1fr; }
      #profile { max-height: 220px; border-right: none; border-top: 1px solid var(--line); }
    }
    #chart {
      height: calc(100vh - 70px);
      min-height: 420px;
      border-right: 1px solid var(--line);
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
      padding: 8px 8px 4px;
      font-size: 0.65rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      flex: 0 0 auto;
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
      padding: 10px 14px 20px;
      overflow: auto;
      background: rgba(22, 27, 34, 0.65);
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
    <span class="pill" id="sigPill"><span class="meta">signal</span> <span id="signal">FLAT</span></span>
    <span class="pill" id="sessPill"><span class="meta">session</span> <span id="sessNet">—</span></span>
    <span class="pill" id="modePill"><span class="meta">mode</span> <span id="modeLabel">—</span></span>
    <span class="meta" id="ts">waiting…</span>
  </header>
  <div id="err" class="err" hidden></div>
  <div class="wrap">
    <div id="chart"></div>
    <div id="profile">
      <div class="ph">Depth · USDT</div>
      <div id="profileLadder"></div>
    </div>
    <aside>
      <h2>Session PnL (paper)</h2>
      <div class="pos-box empty" id="sessionBox">no closed trades yet</div>
      <h2>Position</h2>
      <div class="pos-box empty" id="paperPos">flat — waiting for wall bounce</div>
      <h2>Live position</h2>
      <div class="pos-box empty" id="livePos">—</div>
      <h2>Imbalance (band)</h2>
      <div class="bar-wrap"><div class="bar-bid" id="imbBid"></div><div class="bar-ask" id="imbAsk"></div></div>
      <div class="meta" style="margin-top:6px" id="imbDetail">—</div>
      <h2>Recent paper trades</h2>
      <table>
        <thead><tr><th>side</th><th>exit</th><th>why</th><th>pnl%</th></tr></thead>
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
    const series = chart.addAreaSeries({
      lineColor: "#58a6ff",
      topColor: "rgba(88,166,255,0.28)",
      bottomColor: "rgba(88,166,255,0.02)",
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
        price, color, title, lineWidth: width || 1,
        lineStyle: style != null ? style : LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
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
        const pd = s.mid >= 1000 ? 2 : 4;
        document.getElementById("mid").textContent = fmt(s.mid, pd);
        document.getElementById("spread").textContent = fmt(s.spread, s.mid >= 1000 ? 2 : 5);
        const bidPct = Math.max(0, Math.min(100, (s.imbalance || 0.5) * 100));
        const askPct = 100 - bidPct;
        document.getElementById("imb").textContent =
          bidPct.toFixed(1) + "% bid / " + askPct.toFixed(1) + "% ask";
        document.getElementById("imbBid").style.width = bidPct.toFixed(1) + "%";
        document.getElementById("imbAsk").style.width = askPct.toFixed(1) + "%";
        let imbDetail =
          `bid vol ${fmt(s.bid_vol, 2)} · ask vol ${fmt(s.ask_vol, 2)}`;
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
        const sessBox = document.getElementById("sessionBox");
        const n = sess.trades || 0;
        const feeSrc = sess.fee_source === "binance"
          ? `Binance ${sess.fee_mode || "taker"} (m ${(Number(sess.maker_pct||0)).toFixed(4)}% · t ${(Number(sess.taker_pct||0)).toFixed(4)}%)`
          : `fallback (no API keys)`;
        if (n === 0 && !(s.paper && s.paper.side)) {
          sessBox.className = "pos-box empty";
          sessBox.innerHTML =
            `no closed trades yet<br><span class="meta">fees ${Number(sess.fee_rt_pct||0).toFixed(4)}%/RT · ${feeSrc}</span>`;
        } else {
          sessBox.className = "pos-box";
          const u = sess.unrealized_pct || 0;
          const uCls = u >= 0 ? "win" : "loss";
          sessBox.innerHTML =
            `closed <strong>${n}</strong> · W/L ${sess.wins || 0}/${sess.losses || 0}<br>` +
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
        if (paper.side) {
          const pnl = paper.pnl_pct;
          const pnlCls = pnl >= 0 ? "win" : "loss";
          paperBox.className = "pos-box";
          const peak = paper.peak_pnl_pct != null ? paper.peak_pnl_pct : pnl;
          const beOn = !!paper.be_locked;
          const needFee = Number(s.session && s.session.fee_rt_pct != null ? s.session.fee_rt_pct : 0.1);
          const beLine = beOn
            ? `<span class="win">BE LOCKED</span> @ ${fmt(paper.entry, pd)}`
            : `<span class="meta">BE pending</span> (need ~+${Math.max(0, needFee - pnl).toFixed(3)}% more / fees)`;
          const src = paper.source === "live" ? "LIVE" : "paper";
          const qtyBit = paper.qty ? ` · qty ${fmt(paper.qty, 4)}` : "";
          paperBox.innerHTML =
            `<span class="${paper.side === "long" ? "bid" : "ask"}">${paper.side.toUpperCase()}</span>` +
            ` @ <span class="entry">${fmt(paper.entry, pd)}</span>` +
            ` <span class="meta">[${src}]</span>${qtyBit}<br>` +
            `TP ${fmt(paper.tp, pd)} · SL ${fmt(paper.sl, pd)}` +
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
          return `
          <tr>
            <td class="${t.side === "long" ? "bid" : "ask"}">${t.side}</td>
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
          addLine(w.price, `rgba(63,185,80,${0.35 + 0.55 * (w.notional / maxN)})`,
            "B " + Math.round(w.notional / 1000) + "k", w.notional / maxN > 0.6 ? 2 : 1);
        }
        for (const w of (s.ask_walls || []).slice(0, 4)) {
          addLine(w.price, `rgba(248,81,73,${0.35 + 0.55 * (w.notional / maxN)})`,
            "A " + Math.round(w.notional / 1000) + "k", w.notional / maxN > 0.6 ? 2 : 1);
        }
        if (paper.side) {
          addLine(paper.entry, "#d2a8ff", "ENTRY", 2);
          // Always show BE target at entry; solid gold when locked
          if (paper.be_locked) {
            addLine(paper.entry, "#e3b341", "BE ✓", 2, LightweightCharts.LineStyle.Solid);
          } else {
            addLine(paper.entry, "rgba(227,179,65,0.55)", "BE", 1, LightweightCharts.LineStyle.SparseDotted);
          }
          addLine(paper.tp, "#3fb950", "TP", 1, LightweightCharts.LineStyle.Dashed);
          const slLabel = paper.be_locked && Math.abs(paper.sl - paper.entry) / paper.entry < 1e-8
            ? "SL=BE" : "SL";
          addLine(paper.sl, "#f85149", slLabel, 1, LightweightCharts.LineStyle.Dashed);
        }
        if (live.side && live.entry) {
          addLine(live.entry, "#ffa657", "LIVE", 2, LightweightCharts.LineStyle.SparseDotted);
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
    ) -> None:
        self.symbol = symbol.upper()
        self.limit = limit
        self.walls = walls
        self.band_pct = band_pct
        self.trail_sec = trail_sec
        self.sample_sec = sample_sec
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
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

        self._lock = threading.Lock()
        self._trail: deque[dict[str, float]] = deque()
        self._bb_trail: deque[dict[str, float]] = deque()
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
        self._signal = "flat"
        self._block_reason = "starting…"
        self._cooldown_until = 0.0
        self._bb_next = 0.0
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
        # Trade when inside OR slightly outside (pad). Hard breakout still blocked.
        tradeable = near
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

    def _pct_tp_sl(self, side: str, entry: float) -> tuple[float, float]:
        # Prefer fee-aware distance over tiny legacy tp_pct
        tp_d = max(self.tp_pct, self._min_tp_dist_pct())
        if side == "long":
            return entry * (1 + tp_d / 100), entry * (1 - self.sl_pct / 100)
        return entry * (1 - tp_d / 100), entry * (1 + self.sl_pct / 100)

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
        """Dynamic TP/SL: TP = opposite wall far enough to cover fees; SL = support."""
        fallback_tp, fallback_sl = self._pct_tp_sl(side, entry)
        buf = self.sl_buffer_pct / 100
        need = self._min_tp_dist_pct()
        note = "wall"

        if side == "long":
            ask_w = self._reward_wall(ask_walls, above=True, mid=mid, min_dist_pct=need)
            if ask_w is None:
                ask_w = self._nearest_wall(ask_walls, below=False, mid=mid)
            bid_w = self._nearest_wall(bid_walls, below=True, mid=min(mid, entry))
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

            if bid_w:
                sl = bid_w["price"] * (1 - buf)
            elif best_bid:
                sl = best_bid * (1 - buf)
                note = note if "pct" in note else "wall+bbo"
            else:
                sl = fallback_sl
            sl = min(sl, mid * (1 - buf))
            return tp, sl, note

        # short
        bid_w = self._reward_wall(bid_walls, above=False, mid=mid, min_dist_pct=need)
        if bid_w is None:
            bid_w = self._nearest_wall(bid_walls, below=True, mid=mid)
        ask_w = self._nearest_wall(ask_walls, below=False, mid=max(mid, entry))
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

        if ask_w:
            sl = ask_w["price"] * (1 + buf)
        elif best_ask:
            sl = best_ask * (1 + buf)
            note = note if "pct" in note else "wall+bbo"
        else:
            sl = fallback_sl
        sl = max(sl, mid * (1 + buf))
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
            "qty_str": qty_str,
            "order_id": order_id,
            "pnl_pct": 0.0,
            "peak_pnl_pct": 0.0,
            "peak_mid": fill_entry,
            "armed": False,
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
        if not self.dry_run and self.api_key and self.filt:
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
        fee = self.fee_rt_pct
        net = gross - fee
        trade = {
            "side": side,
            "entry": pos["entry"],
            "exit": mark,
            "pnl_pct": gross,
            "fee_pct": fee,
            "net_pct": net,
            "why": why,
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
        # Shorter pause after SL so the bot can re-arm; longer after wins
        cd = self.cooldown_sec
        if why == "sl":
            cd = min(cd, 5.0)
        self._cooldown_until = now + cd
        self._signal = "flat"
        self._block_reason = f"cooldown {cd:g}s after {why}"

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

        # Refresh TP/SL from live book each tick (ratchet SL in favor only)
        if self.exits == "wall":
            tp, sl, note = self._wall_tp_sl(
                side, entry, mid, bid_walls, ask_walls, best_bid, best_ask
            )
            pos["tp"] = tp
            pos["exits"] = note
            if side == "long":
                pos["sl"] = max(float(pos["sl"]), sl)  # trail up only
            else:
                pos["sl"] = min(float(pos["sl"]), sl)  # trail down only

        pnl = self._pnl_pct(entry, mid, side)
        pos["pnl_pct"] = pnl
        if pnl > float(pos.get("peak_pnl_pct", 0.0)):
            pos["peak_pnl_pct"] = pnl
        if side == "long":
            pos["peak_mid"] = max(float(pos.get("peak_mid", mid)), mid)
        else:
            pos["peak_mid"] = min(float(pos.get("peak_mid", mid)), mid)
        # Soft-exit arm: need fee-covered profit. BE arm: earlier (min_lock).
        soft_arm_need = max(self.min_lock_pct, self.fee_rt_pct if self.net_exits else 0.0)
        be_arm_need = self.min_lock_pct
        if pnl >= soft_arm_need:
            pos["armed"] = True
        be_ready = pnl >= be_arm_need
        pos["be_locked"] = False

        # Protect: BE as soon as slightly green; trail once fee-covered (armed)
        protect_note = []
        if self.protect_be and be_ready:
            if side == "long":
                if float(pos["sl"]) < entry:
                    pos["sl"] = entry
                protect_note.append("BE")
                pos["be_locked"] = True
            else:
                if float(pos["sl"]) > entry:
                    pos["sl"] = entry
                protect_note.append("BE")
                pos["be_locked"] = True
        if self.protect_trail and pos.get("armed"):
            peak_mid = float(pos["peak_mid"])
            if side == "long":
                trail_sl = peak_mid * (1 - self.rev_pct / 100)
                if trail_sl > entry:
                    pos["sl"] = max(float(pos["sl"]), trail_sl)
                    protect_note.append("trail")
            else:
                trail_sl = peak_mid * (1 + self.rev_pct / 100)
                if trail_sl < entry:
                    pos["sl"] = min(float(pos["sl"]), trail_sl)
                    protect_note.append("trail")
        if protect_note:
            base = str(pos.get("exits") or "wall").split("+")[0]
            pos["exits"] = base + "+" + "+".join(protect_note)

        held = now - float(pos.get("opened_at", now))
        soft_ok = held >= self.min_hold_sec
        # Soft exits only if net would be >= 0 (covers fee estimate)
        can_soft = soft_ok and (not self.net_exits or self._covers_fees(pnl))

        # Proactive: lock green when sense flips or price reverses from peak
        if pos.get("armed") and pnl > 0 and can_soft:
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

        # Was green (fee-covered), price crossed back through entry
        if self.giveback_exit and pos.get("armed") and pnl <= 0 and soft_ok:
            self._close_paper(mid, now, "give")
            return

        if side == "long":
            if mid >= pos["tp"]:
                self._close_paper(mid, now, "tp")
            elif mid <= pos["sl"]:
                self._close_paper(mid, now, "sl")
        else:
            if mid <= pos["tp"]:
                self._close_paper(mid, now, "tp")
            elif mid >= pos["sl"]:
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
    ) -> str:
        bids = all_bids if all_bids is not None else bid_walls
        asks = all_asks if all_asks is not None else ask_walls

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
        # Re-evaluate BB vs *current* mid each tick (avoids stale breakout lock)
        self._bb = self._bb_regime_for_mid(mid)
        if not self._bb.get("tradeable", True):
            self._block_reason = f"regime {self._bb.get('label', '?')} (need inside/near BB)"
            return "flat"

        bid_w = self._nearest_wall(bids, below=True, mid=mid)
        ask_w = self._nearest_wall(asks, below=False, mid=mid)
        need = self._min_tp_dist_pct()
        # Reward wall from FULL depth so fee+edge targets are not dropped
        long_tp = self._reward_wall(asks, above=True, mid=mid, min_dist_pct=need)
        short_tp = self._reward_wall(bids, above=False, mid=mid, min_dist_pct=need)

        best_bid = max((w["price"] for w in bids), default=None)
        best_ask = min((w["price"] for w in asks), default=None)

        near_bid = bid_w and abs(bid_w["dist_pct"]) <= self.touch_pct
        near_ask = ask_w and abs(ask_w["dist_pct"]) <= self.touch_pct

        if near_bid and imb >= self.imb_long and long_tp is not None:
            self._open_paper(
                "long", mid, bid_w["price"], now, "bid-wall+imb",
                bid_walls=bids, ask_walls=asks,
                best_bid=best_bid, best_ask=best_ask,
            )
            self._block_reason = ""
            return "long"
        if near_ask and imb <= self.imb_short and short_tp is not None:
            self._open_paper(
                "short", mid, ask_w["price"], now, "ask-wall+imb",
                bid_walls=bids, ask_walls=asks,
                best_bid=best_bid, best_ask=best_ask,
            )
            self._block_reason = ""
            return "short"

        # Explain why flat (first matching reason)
        reasons = []
        if not near_bid and not near_ask:
            reasons.append(f"no wall within {self.touch_pct:g}%")
        elif near_bid and imb < self.imb_long:
            reasons.append(f"imb {imb*100:.1f}% < long {self.imb_long*100:.0f}%")
        elif near_ask and imb > self.imb_short:
            reasons.append(f"imb {imb*100:.1f}% > short {self.imb_short*100:.0f}%")
        if near_bid and long_tp is None:
            reasons.append(f"no ask TP ≥{need:g}% away")
        if near_ask and short_tp is None:
            reasons.append(f"no bid TP ≥{need:g}% away")
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
                        "order_error": self._last_order_error,
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
    p.add_argument("--sl-pct", type=float, default=0.06, help="fallback / pct-mode SL %%")
    p.add_argument("--max-tp-pct", type=float, default=0.25,
                   help="cap dynamic TP distance from entry %%")
    p.add_argument("--sl-buffer-pct", type=float, default=0.005,
                   help="place SL this %% beyond the support/resistance wall")
    p.add_argument("--touch-pct", type=float, default=0.08, help="max dist %% to wall for entry")
    p.add_argument("--min-wall-usdt", type=float, default=40_000.0, help="min wall notional")
    p.add_argument("--imb-long", type=float, default=0.55, help="min bid imbalance for long")
    p.add_argument("--imb-short", type=float, default=0.45, help="max bid imbalance for short")
    p.add_argument("--bb-period", type=int, default=20)
    p.add_argument("--bb-std", type=float, default=2.0)
    p.add_argument("--bb-interval", default="5m",
                   help="BB regime timeframe (5m = fewer choppy entries than 1m)")
    p.add_argument("--bb-pad-pct", type=float, default=0.20,
                   help="allow entries this %% outside BB bands (re-entry after stop)")
    p.add_argument("--cooldown-sec", type=float, default=8.0,
                   help="pause after close (SL uses min(this, 5s))")
    p.add_argument("--min-edge-pct", type=float, default=0.04,
                   help="extra %% beyond fees required to opposite wall before entry")
    p.add_argument("--min-hold-sec", type=float, default=3.0,
                   help="min seconds in trade before soft exits (flip/rev/give)")
    p.add_argument("--net-exits", action=argparse.BooleanOptionalAction, default=True,
                   help="only soft-exit when gross pnl covers fee estimate")
    p.add_argument("--protect-be", action=argparse.BooleanOptionalAction, default=True,
                   help="once in fee-covered profit, move SL to breakeven")
    p.add_argument("--protect-trail", action=argparse.BooleanOptionalAction, default=True,
                   help="once armed, trail SL under/over peak by --rev-pct")
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
    )
    # Pull Binance commission rates immediately (needs API keys in .env)
    state._refresh_fees(0.0)

    worker = threading.Thread(target=state.loop, name="depth", daemon=True)
    worker.start()

    handler = make_handler(state, args.ui_ms)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
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
        f"exits={args.exits}  TP% {args.tp_pct:g}  SL% {args.sl_pct:g}  "
        f"max-tp {args.max_tp_pct:g}%  touch {args.touch_pct:g}%  "
        f"min-wall {args.min_wall_usdt:,.0f} USDT"
    )
    print(
        f"proactive: flip={args.flip_exit} rev={args.rev_exit} "
        f"giveback={args.giveback_exit}  arm>={args.min_lock_pct:g}%  rev={args.rev_pct:g}%"
    )
    print(
        f"protect: BE={args.protect_be} trail={args.protect_trail} "
        f"(arms when pnl covers fees)"
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
