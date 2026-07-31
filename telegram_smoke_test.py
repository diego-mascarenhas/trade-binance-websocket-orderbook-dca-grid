#!/usr/bin/env python3
"""Fire sample Telegram alerts to public channel and/or private ops.

Usage (on the VPS, from repo root):
  python3 telegram_smoke_test.py              # config + public samples
  python3 telegram_smoke_test.py --public
  python3 telegram_smoke_test.py --ops        # detailed Vol/qty to TELEGRAM_CHAT_ID
  python3 telegram_smoke_test.py --all
  python3 telegram_smoke_test.py --tag OPEN   # one public tag
"""

from __future__ import annotations

import argparse
import os
import sys


def _load_env() -> None:
    from orderbook_dca_grid import load_env_file

    load_env_file(None)


def _print_config() -> None:
    import telegram_notify as tg
    import pumpstall_telegram as pst

    ops = tg._chat_id()  # noqa: SLF001
    pub = pst.chat_id()
    print("Telegram config")
    print(f"  token:              {'yes' if tg._token() else 'NO'}")  # noqa: SLF001
    print(f"  TELEGRAM_CHAT_ID:   {ops or '(empty)'}  ← private ctl/bank")
    print(f"  public chat:        {pub or '(empty)'}  ← Pumpstall channel")
    print(f"  ops_is_public:      {tg._ops_is_public_channel()}")  # noqa: SLF001
    print(f"  ops_trade_alerts:   {tg._ops_trade_alerts()}")  # noqa: SLF001
    print(f"  public configured:  {pst.is_configured()} ({pst.config_status()})")
    print()


def _send_public_suite(symbol: str) -> None:
    import telegram_notify as tg
    import pumpstall_telegram as pst

    print(f"==> Public samples → {pst.chat_id()}")
    demo = {
        "symbol": symbol,
        "change_24h": 8.5,
        "pump_7d_pct": 116,
        "score": 84.1,
        "near_high_pct": 90,
        "near_regime_pct": 90,
        "sharp_pct": 54,
        "stall_score": 47,
        "ask_walls": 8,
        "ask_span_pct": 14.1,
        "wall_prices": [4.25, 4.28, 4.32],
        "note": "smoke-test IDEAL (not a live signal)",
    }
    ok = pst.notify_open_hit(demo)
    print(f"  IDEAL:  {'ok' if ok else 'FAIL'}")

    for tag, pnl, entry, mark in (
        ("OPEN", None, 4.25, 4.25),
        ("DCA", -0.12, 4.25, 4.30),
        ("TP", 0.08, 4.25, 4.20),
        ("BE", 0.05, 4.25, 4.22),
        ("TRAIL", 0.10, 4.25, 4.18),
        ("SL", -0.20, 4.25, 4.35),
        ("CLOSE", 0.15, 4.25, 4.15),
    ):
        tg._post_public_tag(  # noqa: SLF001
            tag,
            symbol,
            "SHORT",
            pnl_usdt=pnl,
            notional=21.0,
            leverage=10,
            entry=entry,
            mark=mark,
        )
        print(f"  #{tag}:   queued")

    report = pst.format_daily_summary()
    ok_r = pst._send_html(report)  # noqa: SLF001
    print(f"  REPORT: {'ok' if ok_r else 'FAIL'}")


def _send_ops_suite(symbol: str) -> None:
    import telegram_notify as tg

    # Force detailed ops path for this process only.
    os.environ["TELEGRAM_OPS_TRADE_ALERTS"] = "1"
    print(f"==> Ops samples → {tg._chat_id()}")  # noqa: SLF001
    if not tg.is_configured():
        print("  FAIL: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return
    if tg._ops_is_public_channel():  # noqa: SLF001
        print("  WARN: ops chat == public channel — size-bearing msgs may be blocked")

    tg.notify_grid_armed(symbol, "SHORT", 5, grid_vol_usdt=100.0, leverage=10)
    print("  grid_armed: ok")
    tg.notify_position_open(
        symbol, "SHORT", 5.0, 4.25, vol_usdt=21.24, leverage=10, pnl_usdt=0.03,
    )
    print("  position_open: ok")
    tg.notify_dca_filled(
        symbol, "SHORT", 2.0, 4.30, 7.0, 4.27,
        vol_usdt=30.0, leverage=10, pnl_usdt=-0.10, mark=4.30,
    )
    print("  dca_filled: ok")
    tg.notify_tp1_filled(
        symbol, "SHORT", 3.5, 1.5, 4.25,
        tp1_price=4.20, leverage=10, pnl_usdt=0.12,
    )
    print("  tp1_filled: ok")
    tg.notify_profit_lock_sl(
        symbol, "SHORT", 1.5, 4.25, 4.237,
        closed_pct=70, trigger="smoke-test BE", leverage=10, pnl_usdt=0.05,
        hashtag="#BE",
    )
    print("  BE: ok")
    tg.notify_trail_started(
        symbol, "SHORT", 1.5, 4.18, 0.8,
        entry=4.25, leverage=10, pnl_usdt=0.10,
    )
    print("  TRAIL: ok")
    tg.notify_position_closed(
        symbol, "SHORT",
        vol_usdt=21.24, leverage=10, pnl_usdt=0.15,
        entry=4.25, mark=4.15, reason="smoke-test close",
    )
    print("  CLOSE: ok")
    tg.send_bot(f"{symbol} smoke-test · private ctl path OK")
    print("  bot ctl ping: ok")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke-test Telegram public/ops alerts")
    p.add_argument("--public", action="store_true", help="Send public channel samples")
    p.add_argument("--ops", action="store_true", help="Send private ops samples (with size)")
    p.add_argument("--all", action="store_true", help="Public + ops")
    p.add_argument(
        "--tag",
        choices=("OPEN", "DCA", "TP", "BE", "TRAIL", "SL", "CLOSE"),
        help="Send a single public #TAG",
    )
    p.add_argument("--symbol", default="TESTUSDT", help="Symbol label in samples")
    return p.parse_args()


def main() -> int:
    _load_env()
    args = parse_args()
    _print_config()

    do_public = args.public or args.all or args.tag
    do_ops = args.ops or args.all
    if not do_public and not do_ops:
        # Default: public suite (what the channel should show).
        do_public = True

    if args.tag:
        import telegram_notify as tg

        tg._post_public_tag(  # noqa: SLF001
            args.tag, args.symbol, "SHORT",
            pnl_usdt=0.1 if args.tag != "SL" else -0.2,
            notional=21.0, leverage=10, entry=4.25, mark=4.20,
        )
        print(f"Sent public #{args.tag} for {args.symbol}")
        return 0

    if do_public:
        _send_public_suite(args.symbol)
    if do_ops:
        _send_ops_suite(args.symbol)

    print("\nDone. Check Telegram (public channel and/or private ops).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
