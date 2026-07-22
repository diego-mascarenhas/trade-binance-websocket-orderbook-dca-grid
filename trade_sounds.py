"""Local trade sounds via macOS afplay (non-blocking).

Mute without restarting bots:
    ./obscalp-sounds mute
    ./obscalp-sounds unmute
    ./obscalp-sounds status

Or env: OB_SOUNDS=0
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

# macOS system sounds — override with OB_SOUND_* env paths
_DEFAULT = {
    "entry": "/System/Library/Sounds/Glass.aiff",
    "dca": "/System/Library/Sounds/Pop.aiff",
    "tp": "/System/Library/Sounds/Ping.aiff",
    "sl": "/System/Library/Sounds/Sosumi.aiff",
    "pick": "/System/Library/Sounds/Hero.aiff",
    "cycle_end": "/System/Library/Sounds/Submarine.aiff",
}

_ENV = {
    "entry": "OB_SOUND_ENTRY",
    "dca": "OB_SOUND_DCA",
    "tp": "OB_SOUND_TP",
    "sl": "OB_SOUND_SL",
    "pick": "OB_SOUND_PICK",
    "cycle_end": "OB_SOUND_CYCLE_END",
}

_ROOT = Path(__file__).resolve().parent
_MUTE_FLAG = _ROOT / ".run" / "sounds_muted"
_PACMAN_DIR_LOCAL = _ROOT / "sounds" / "pacman"
_PACMAN_DIR_ICLOUD = Path(
    "/Users/magoo/Library/Mobile Documents/F3LWYJ7GM7~com~apple~mobilegarageband/Documents"
    "/PAC-MAN Game Sound Effects/mp3"
)

# OB_SOUND_PACK=pacman  →  use these files from PACMAN_SFX_DIR (or local sounds/pacman)
_PACMAN_FILES = {
    "entry": "01. Credit Sound.mp3",
    "dca": "03. PAC-MAN - Eating The Pac-dots.mp3",
    "tp": "05. Extend Sound.mp3",
    "sl": "15. Fail.mp3",
    "pick": "02. Start Music.mp3",
    "cycle_end": "14. Ghost - Return to Home.mp3",
}


def mute_flag_path() -> Path:
    return _MUTE_FLAG


def is_muted() -> bool:
    """True if muted via flag file or OB_SOUNDS=0."""
    if _MUTE_FLAG.exists():
        return True
    return os.getenv("OB_SOUNDS", "1").strip().lower() in ("0", "false", "no", "off")


def set_muted(muted: bool) -> Path:
    """Create/remove .run/sounds_muted so running bots pick it up immediately."""
    _MUTE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if muted:
        _MUTE_FLAG.write_text("1\n", encoding="utf-8")
    else:
        _MUTE_FLAG.unlink(missing_ok=True)
    return _MUTE_FLAG


def sounds_enabled() -> bool:
    return not is_muted()


def _pacman_dir() -> Path:
    custom = os.getenv("PACMAN_SFX_DIR", "").strip()
    if custom:
        return Path(custom)
    if _PACMAN_DIR_LOCAL.exists():
        return _PACMAN_DIR_LOCAL
    return _PACMAN_DIR_ICLOUD


def pacman_available() -> bool:
    d = _pacman_dir()
    return (d / _PACMAN_FILES["entry"]).exists()


def _effective_pack() -> str:
    pack = os.getenv("OB_SOUND_PACK", "").strip().lower()
    if pack in ("0", "system", "off", "none"):
        return ""
    if pack:
        return pack
    if (_PACMAN_DIR_LOCAL / _PACMAN_FILES["entry"]).exists():
        return "pacman"
    return ""


def _resolve_path(event: str) -> str | None:
    event = event.lower()
    pack = _effective_pack()
    if pack == "pacman":
        path = _pacman_dir() / _PACMAN_FILES.get(event, "")
        if path.exists():
            return str(path)
    env_key = _ENV.get(event, "")
    custom = os.getenv(env_key, "").strip() if env_key else ""
    if custom and Path(custom).exists():
        return custom
    default = _DEFAULT.get(event, "")
    if default and Path(default).exists():
        return default
    return None


def _afplay(path: str) -> None:
    try:
        subprocess.run(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def play_sound(event: str, *, block: bool = False) -> str | None:
    """Play sound for: entry, dca, tp, sl, pick, cycle_end."""
    if not sounds_enabled():
        return None
    path = _resolve_path(event.lower())
    if not path:
        return None
    if block:
        _afplay(path)
    else:
        threading.Thread(target=_afplay, args=(path,), daemon=True).start()
    return path


def play_sound_sync(event: str) -> str | None:
    """Block until the sound finishes (handy for quick tests)."""
    return play_sound(event, block=True)


def play_close_sound(net_usdt: float) -> None:
    play_sound("tp" if net_usdt > 0 else "sl")


def sound_pack_label() -> str:
    pack = _effective_pack()
    if pack == "pacman":
        return "pacman"
    if any(os.getenv(k, "").strip() for k in _ENV.values()):
        return "custom"
    return "system"


def _play_sync(event: str) -> str | None:
    return play_sound(event, block=True)


def _cmd_status() -> int:
    muted = is_muted()
    reasons = []
    if _MUTE_FLAG.exists():
        reasons.append(f"flag {_MUTE_FLAG}")
    env = os.getenv("OB_SOUNDS", "1").strip()
    if env.lower() in ("0", "false", "no", "off"):
        reasons.append(f"OB_SOUNDS={env}")
    state = "MUTED" if muted else "ON"
    extra = f" ({', '.join(reasons)})" if reasons else ""
    print(f"sounds: {state}{extra} · pack={sound_pack_label()}")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    import time

    parser = argparse.ArgumentParser(
        description="Trade sounds — play, mute, unmute (mute applies live to running bots)",
    )
    _EVENTS = ("entry", "dca", "tp", "sl", "pick", "cycle_end", "all")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        help="mute | unmute | status | on | off | entry|dca|tp|sl|pick|cycle_end|all",
    )
    parser.add_argument("-l", "--list", action="store_true", help="Show resolved paths")
    args = parser.parse_args()
    cmd = str(args.command).strip().lower()

    if args.list:
        print(f"pack: {sound_pack_label()}")
        print(f"muted: {is_muted()}")
        for name in ("entry", "dca", "tp", "sl", "pick", "cycle_end"):
            print(f"  {name}: {_resolve_path(name)}")
        raise SystemExit(0)

    if cmd in ("mute", "off", "disable"):
        set_muted(True)
        print(f"sounds: MUTED ({_MUTE_FLAG})")
        raise SystemExit(0)
    if cmd in ("unmute", "on", "enable"):
        set_muted(False)
        print("sounds: ON")
        raise SystemExit(0)
    if cmd in ("status", "state"):
        raise SystemExit(_cmd_status())

    if cmd not in _EVENTS:
        print(
            f"Unknown command '{cmd}'. Use: mute|unmute|status|{'|'.join(_EVENTS)}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if is_muted():
        print("sounds are MUTED — run: obscalp-sounds unmute", file=sys.stderr)
        raise SystemExit(1)

    if cmd == "all":
        for name in ("entry", "dca", "tp", "sl", "pick", "cycle_end"):
            path = _play_sync(name)
            print(f"{name}: {path or 'MISSING'}")
            time.sleep(0.4)
    else:
        path = _play_sync(cmd)
        if not path:
            print(f"No sound file for '{cmd}'", file=sys.stderr)
            raise SystemExit(1)
        print(f"{cmd} ({sound_pack_label()}): {path}")
