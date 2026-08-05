#!/usr/bin/env python3
"""Telegram remote control: start/stop/status per symbol (orders & positions unchanged on stop)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
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

PUMP_EARLY = "pump-stall-watch-early"
PUMP_STRICT = "pump-stall-watch"
SYSTEMCTL = "/bin/systemctl"


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


def _html_to_plain(html: str) -> str:
    text = (
        str(html)
        .replace("<b>", "")
        .replace("</b>", "")
        .replace("<i>", "")
        .replace("</i>", "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"<[^>]+>", "", text)


def _systemctl(*args: str) -> tuple[int, str]:
    """Run passwordless sudo systemctl (see /etc/sudoers.d/pump-stall-ctl)."""
    cmd = ["sudo", "-n", SYSTEMCTL, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out


def _unit_state(unit: str) -> str:
    code, out = _systemctl("is-active", unit)
    active = out.splitlines()[0].strip() if out else "unknown"
    if code != 0 and active not in ("active", "inactive", "failed", "activating"):
        return f"error ({out or code})"
    _c2, en = _systemctl("is-enabled", unit)
    enabled = en.splitlines()[0].strip() if en else "?"
    return f"{active} · {enabled}"


def _pump_status() -> str:
    early = _unit_state(PUMP_EARLY)
    strict = _unit_state(PUMP_STRICT)
    if early.startswith("active"):
        mode = "EARLY"
    elif strict.startswith("active"):
        mode = "STRICT"
    else:
        mode = "STOPPED"
    return (
        f"Pump-stall · {mode}\n"
        f"early:  {early}\n"
        f"strict: {strict}"
    )


def _pump_stop() -> str:
    lines: list[str] = []
    for unit in (PUMP_EARLY, PUMP_STRICT):
        code, out = _systemctl("stop", unit)
        if code != 0 and out and "not loaded" not in out.lower():
            lines.append(f"stop {unit}: {out or code}")
    lines.append(_pump_status())
    return "\n".join(lines)


def _pump_start(profile: str | None = None) -> str:
    """Start early or strict. If profile is None, start whichever is enabled."""
    if profile == "early":
        target = PUMP_EARLY
    elif profile == "strict":
        target = PUMP_STRICT
    else:
        _c, early_en = _systemctl("is-enabled", PUMP_EARLY)
        if (early_en or "").strip() == "enabled":
            target = PUMP_EARLY
        else:
            target = PUMP_STRICT
    # Prefer a single active profile.
    other = PUMP_STRICT if target == PUMP_EARLY else PUMP_EARLY
    _systemctl("stop", other)
    code, out = _systemctl("start", target)
    if code != 0:
        return f"❌ start {target} failed: {out or code}\n{_pump_status()}"
    return f"✅ started {target}\n{_pump_status()}"


def _pump_switch(profile: str) -> str:
    if profile == "early":
        on, off = PUMP_EARLY, PUMP_STRICT
    elif profile == "strict":
        on, off = PUMP_STRICT, PUMP_EARLY
    else:
        return "Usage: /pump early | /pump strict"
    steps = [
        ("stop", off),
        ("disable", off),
        ("enable", on),
        ("start", on),
    ]
    errors: list[str] = []
    for action, unit in steps:
        code, out = _systemctl(action, unit)
        if code != 0:
            errors.append(f"{action} {unit}: {out or code}")
    if errors:
        return "❌ Switch failed:\n" + "\n".join(errors) + f"\n{_pump_status()}"
    label = "EARLY" if profile == "early" else "STRICT"
    return f"✅ Pump-stall → {label}\n{_pump_status()}"


def _report_private() -> str:
    try:
        import pumpstall_telegram as pst
    except ImportError as exc:
        return f"❌ pumpstall_telegram unavailable: {exc}"
    try:
        html = pst.format_daily_summary(private=True)
    except Exception as exc:  # noqa: BLE001
        return f"❌ Report failed: {exc}"
    return _html_to_plain(html)


def _parse_message(text: str) -> tuple[str, list[str]]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", []
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = [p.strip() for p in parts[1:] if p.strip()]
    return cmd, args


def _handle_boost(args: list[str]) -> str:
    """Manage per-symbol size boost files under .state/boost/."""
    try:
        import size_boost as sb
    except ImportError as exc:
        return f"❌ size_boost unavailable: {exc}"

    if not args or args[0].lower() in ("list", "ls", "all"):
        rows = sb.list_boosts()
        if not rows:
            return (
                "No size boosts active.\n"
                f"Usage: /boost SYMBOL [{sb.default_mult():g}|off]\n"
                "e.g. /boost UBUSDT  → 1.5×  ·  /boost UBUSDT off"
            )
        lines = ["Size boosts (.state/boost/):"]
        for r in rows:
            lines.append(f"  {r['symbol']}  {sb.fmt_mult(r['mult'])}")
        return "\n".join(lines)

    sym = args[0].upper()
    if not sb.normalize_symbol(sym):
        return "Invalid symbol (e.g. UBUSDT)"

    if len(args) == 1:
        try:
            row = sb.set_boost(sym, None)
        except ValueError as exc:
            return f"❌ {exc}"
        return (
            f"✅ Boost {sym} → {sb.fmt_mult(row['mult'])}\n"
            f"File: .state/boost/{sym}.json\n"
            "Applies on next arm (entry + DCA)."
        )

    tok = args[1].lower()
    if tok in ("off", "clear", "del", "delete", "0", "none"):
        if sb.clear(sym):
            return f"✅ Boost cleared for {sym}"
        return f"No boost file for {sym}"

    try:
        mult = float(tok)
    except ValueError:
        return (
            f"Usage: /boost {sym} [{sb.default_mult():g}|off]\n"
            "e.g. /boost UBUSDT 2  ·  /boost UBUSDT off"
        )
    if mult < sb.MIN_MULT:
        if sb.clear(sym):
            return f"✅ Boost cleared for {sym} (mult < {sb.MIN_MULT:g})"
        return f"No boost file for {sym}"
    try:
        row = sb.set_boost(sym, mult)
    except ValueError as exc:
        return f"❌ {exc}"
    return (
        f"✅ Boost {sym} → {sb.fmt_mult(row['mult'])}\n"
        f"File: .state/boost/{sym}.json\n"
        "Applies on next arm (entry + DCA)."
    )


def handle_command(cmd: str, args: list[str]) -> str:
    backend = botctl.detect_backend()

    if cmd in ("/help", "/start_help"):
        return (
            "Bot control commands:\n"
            "/start SYMBOL [long|short|auto] [gate] — start DCA supervisor\n"
            "/fib SYMBOL [long|short|auto] — start FIB micro-grid\n"
            "/stop SYMBOL — stop DCA and/or FIB (orders & position stay)\n"
            "/status SYMBOL — process + trading state\n"
            "/boost [SYMBOL [mult|off]] — size boost (.state/boost/SYMBOL.json)\n"
            "/cleanup SYMBOL — cancel obstage* Stop/TP algos\n"
            "/sweep [SYMBOL] — cancel orphan bot limits/algos when flat\n"
            "/review SYMBOL — DeepSeek situational review\n"
            "/list — all running bots\n"
            "/report — Pumpstall #REPORT in this chat (private)\n"
            "/pump status|start|stop|early|strict|sweep — pump-stall service\n"
            "gate: SHORT only if mid>gate · LONG only if mid<gate\n"
            f"Backend: {backend}"
        )

    if cmd == "/list":
        return botctl.list_status(backend)

    if cmd == "/fib":
        if not args:
            return "Usage: /fib SYMBOL [long|short|auto]  (e.g. /fib LTCUSDT short)"
        sym = args[0].upper()
        direction = args[1].lower() if len(args) > 1 else None
        if direction and direction not in ("long", "short", "auto"):
            return "Direction must be long, short, or auto"
        return botctl.fib_start(sym, direction)

    if cmd in ("/start", "/stop", "/status"):
        action = cmd.lstrip("/")
        if not args:
            return f"Usage: {cmd} SYMBOL  (e.g. {cmd} SXTUSDT)"
        sym = args[0].upper()
        if action == "start":
            direction = None
            gate = None
            for tok in args[1:]:
                low = tok.lower()
                if low in ("long", "short", "auto"):
                    direction = low
                    continue
                try:
                    gate = float(tok)
                except ValueError:
                    return (
                        "Usage: /start SYMBOL [long|short|auto] [gate]\n"
                        "e.g. /start REUSDT short 0.0125"
                    )
            return botctl.start(sym, backend, direction=direction, gate_price=gate)
        if action == "stop":
            return botctl.stop(sym, backend)
        return botctl.status(sym, backend)

    if cmd == "/review":
        if not args:
            return "Usage: /review SYMBOL  (e.g. /review NEARUSDT)"
        try:
            from trade_review import review_symbol
            return review_symbol(args[0].upper())
        except Exception as exc:
            return f"Review failed: {exc}"

    if cmd == "/cleanup":
        if not args:
            return "Usage: /cleanup SYMBOL  (e.g. /cleanup HEIUSDT)"
        return botctl.cleanup(args[0].upper())

    if cmd == "/sweep":
        sym = args[0].upper() if args else None
        return botctl.sweep(sym)

    if cmd == "/report":
        return _report_private()

    if cmd == "/boost":
        return _handle_boost(args)

    if cmd == "/pump":
        action = (args[0].lower() if args else "status").strip()
        if action in ("status", "st"):
            return _pump_status()
        if action == "stop":
            return _pump_stop()
        if action == "start":
            return _pump_start(None)
        if action == "early":
            return _pump_switch("early")
        if action in ("strict", "normal"):
            return _pump_switch("strict")
        if action == "sweep":
            return botctl.sweep(None)
        return (
            "Usage: /pump status|start|stop|early|strict|sweep\n"
            f"{_pump_status()}"
        )

    return "Unknown command. Try /help"


def _authorized(chat: dict) -> bool:
    want = _chat_id()
    if not want:
        return False
    return str(chat.get("id", "")) == str(want)


def poll_once(offset: int) -> int:
    data = _api("getUpdates", timeout=30, offset=offset if offset else None)
    for upd in data.get("result", []):
        uid = int(upd.get("update_id", 0))
        # Confirm each update immediately. A corrupt high watermark in the
        # offset file (max(old, uid+1)) can leave Telegram redelivering the
        # same /start forever → help spam.
        if offset and uid + 1 < offset:
            logger.warning(
                "Offset file ahead of Telegram (%s > %s); rewinding",
                offset,
                uid + 1,
            )
        offset = uid + 1
        _save_offset(offset)
        try:
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
        except Exception:
            logger.exception("Failed handling update %s", uid)
    return offset


def run_daemon(poll_sec: float = 1.0) -> None:
    if not _token() or not _chat_id():
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required in .env", file=sys.stderr)
        sys.exit(1)

    botctl.ROOT  # ensure import side ok
    backend = botctl.detect_backend()
    # No startup Telegram ping — systemd restarts would spam the ops chat.
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
    p = argparse.ArgumentParser(description="Telegram start/stop/status for DCA + FIB bots")
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
