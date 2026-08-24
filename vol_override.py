"""Manual pin for the volatility regime profile (Telegram /pump).

Shared by telegram_botctl (writer) and pump_stall_scan (reader). The scanner
re-reads this every cycle, so /pump takes effect within one refresh without
restarting the unit.

Stand-down (BTC 24h range ≥ VOL_REGIME_BTC_RANGE_PCT) still wins over a pin —
the hot-market brake is not something /pump can switch off.

State: .state/vol_override.json
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

STATE_DIRNAME = ".state"
STATE_FILE = "vol_override.json"

PROFILES = ("early", "strict")


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _path() -> str:
    d = os.path.join(_repo_root(), STATE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, STATE_FILE)


def get() -> str | None:
    """Pinned profile, or None when the vol regime is in charge."""
    path = _path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    prof = str(data.get("profile") or "").strip().lower()
    return prof if prof in PROFILES else None


def set_profile(profile: str | None) -> str | None:
    """Pin ``early``/``strict``; None (or ``auto``) hands control back.

    Returns the stored profile, or None for auto.
    """
    raw = (profile or "").strip().lower()
    if raw in ("", "auto", "off", "none"):
        try:
            os.remove(_path())
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return None
    if raw not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)} or auto")
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "profile": raw,
                "at": time.time(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            fh,
            indent=2,
        )
    return raw


def label() -> str:
    """Short human state for /pump status."""
    prof = get()
    return f"pinned {prof}" if prof else "auto"
