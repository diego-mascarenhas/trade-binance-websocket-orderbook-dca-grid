#!/usr/bin/env python3
"""Telegram remote control: start/stop/status per symbol (orders & positions unchanged on stop)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import botctl
import telegram_notify

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
OFFSET_FILE = ROOT / ".run" / "telegram_botctl.offset"


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _load_offset() -> int:
    if not OFFSET_FILE.exists():
        return 0
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (TypeError, ValueError, OSError):
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset), encoding="utf-8")


def _api(method: str, **params: object) -> dict:
    url = f"https://api.telegram.org/bot{_token()}/{method}"
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=35) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def send_reply(text: str) -> None:
    telegram_notify._send_sync(text[:4096])


def _parse_message(text: str) -> tuple[str, list[str]]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", []
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = [p.strip().upper() for p in parts[1:] if p.strip()]
    return cmd, args


def handle_command(cmd: str, args: list[str]) -> str:
    backend = botctl.detect_backend()

    if cmd in ("/help", "/start_help"):
        return (
            "DCA supervisor commands:\n"
            "/start SYMBOL — start bot (position unchanged)\n"
            "/stop SYMBOL — stop bot (orders & position stay on Binance)\n"
            "/status SYMBOL — process + trading state\n"
            "/list — all running supervisors\n"
            f"Backend: {backend}"
        )

    if cmd == "/list":
        return botctl.list_status(backend)

    if cmd in ("/start", "/stop", "/status"):
        action = cmd.lstrip("/")
        if not args:
            return f"Usage: {cmd} SYMBOL  (e.g. {cmd} SXTUSDT)"
        sym = args[0]
        if action == "start":
            return botctl.start(sym, backend)
        if action == "stop":
            return botctl.stop(sym, backend)
        return botctl.status(sym, backend)

    return "Unknown command. Try /help"


def _authorized(chat: dict) -> bool:
    want = _chat_id()
    if not want:
        return False
    return str(chat.get("id", "")) == str(want)


def poll_once(offset: int) -> int:
    data = _api("getUpdates", timeout=30, offset=offset if offset else None)
    for upd in data.get("result", []):
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        if not _authorized(chat):
            logger.warning("Ignored message from unauthorized chat %s", chat.get("id"))
            continue
        text = msg.get("text") or ""
        cmd, args = _parse_message(text)
        if not cmd:
            continue
        if cmd == "/start" and not args:
            reply = handle_command("/help", [])
        else:
            reply = handle_command(cmd, args)
        send_reply(reply)
    return offset


def run_daemon(poll_sec: float = 1.0) -> None:
    if not _token() or not _chat_id():
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required in .env", file=sys.stderr)
        sys.exit(1)

    botctl.ROOT  # ensure import side ok
    backend = botctl.detect_backend()
    send_reply(f"🤖 DCA control active ({backend}). /help for commands.")
    logger.info("Telegram botctl started (backend=%s)", backend)

    offset = _load_offset()
    try:
        while True:
            try:
                offset = poll_once(offset)
                _save_offset(offset)
            except urllib.error.HTTPError as exc:
                logger.warning("Telegram HTTP error: %s", exc)
                time.sleep(5)
            except Exception as exc:
                logger.exception("Poll error: %s", exc)
                time.sleep(poll_sec)
            else:
                time.sleep(poll_sec)
    except KeyboardInterrupt:
        logger.info("Stopped.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram start/stop/status for DCA supervisors")
    p.add_argument("--poll-sec", type=float, default=1.0)
    p.add_argument("--env-file", default=None)
    return p.parse_args()


def main() -> None:
    from orderbook_dca_grid import load_env_file

    args = parse_args()
    load_env_file(args.env_file)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_daemon(args.poll_sec)


if __name__ == "__main__":
    main()
