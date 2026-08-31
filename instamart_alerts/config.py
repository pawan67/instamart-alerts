"""Environment-backed settings, with a writable layer on top.

`.env` is the floor. Anything the control panel changes is written to
`data/settings.json` and wins over the environment, so the panel can configure a
running install without anyone editing files by hand. The file holds a bot token,
so it is written owner-only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# Swiggy rejects calls that look too far behind the deployed web build. This is
# the fallback; the panel can override it without a redeploy, which is the whole
# point — when Swiggy moves on, the fix is a new number, not new code.
BUILD_VERSION = "2.367.0"
API = "https://www.swiggy.com/api/instamart"

DEFAULT_POLL_MINUTES = 15
DEFAULT_COOLDOWN_HOURS = 24.0
# How long to let the WAF challenge script run before giving up on it. A small
# VPS is much slower at this than a laptop.
DEFAULT_BOOTSTRAP_SECONDS = 30

# How Instamart calls leave the machine.
#   http    — hand the browser's token to httpx. One second a poll.
#   browser — issue every call from inside the page that solved the challenge.
#             Slow (a Chromium per pass) but the fingerprint matches the token.
#   auto    — http, falling back to browser on the last retry.
TRANSPORTS = ("auto", "http", "browser")
DEFAULT_TRANSPORT = "auto"

# Recipients are stored as one string so `.env`, `settings.json` and the panel
# all speak the same format. Commas, spaces and newlines all separate.
_SEPARATORS = re.compile(r"[,;\s]+")


def parse_chat_ids(raw: str | list | tuple | None) -> tuple[str, ...]:
    """'111, 222' or ['111','222'] -> ('111', '222'), order kept, dupes dropped."""
    if raw is None:
        return ()
    parts = raw if isinstance(raw, (list, tuple)) else _SEPARATORS.split(str(raw))
    return tuple(dict.fromkeys(str(p).strip() for p in parts if str(p).strip()))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    # One or more Telegram chat ids. Every alert goes to all of them.
    chat_id: str
    area: str
    proxy: str | None
    data_dir: Path
    watchlist_path: Path
    headless: bool
    # Skips Mini App signature checks so the UI can be opened in a normal
    # browser. Never enable on anything reachable from the internet.
    dev_mode: bool = False
    # Background poller, driven by the control panel.
    poll_minutes: int = DEFAULT_POLL_MINUTES
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS
    poll_enabled: bool = False
    build_version: str = BUILD_VERSION
    bootstrap_seconds: int = DEFAULT_BOOTSTRAP_SECONDS
    transport: str = DEFAULT_TRANSPORT

    @property
    def chat_ids(self) -> tuple[str, ...]:
        return parse_chat_ids(self.chat_id)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_ids)


def overrides_path(data_dir: Path) -> Path:
    """Runtime settings the web UI can change, layered over .env."""
    return data_dir / "settings.json"


def read_overrides(data_dir: Path) -> dict:
    p = overrides_path(data_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_overrides(data_dir: Path, values: dict) -> dict:
    merged = read_overrides(data_dir) | values
    path = overrides_path(data_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True))
    try:
        tmp.chmod(0o600)  # it can hold a bot token
    except OSError:
        pass
    tmp.replace(path)
    return merged


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load() -> Settings:
    data_dir = Path(os.getenv("IM_DATA_DIR") or ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    over = read_overrides(data_dir)

    def pick(key: str, env: str) -> str:
        return str(over.get(key) or os.getenv(env, "") or "").strip()

    return Settings(
        bot_token=pick("bot_token", "TELEGRAM_BOT_TOKEN"),
        # The panel may have written a list; normalise either shape to a string.
        chat_id=", ".join(
            parse_chat_ids(over.get("chat_id"))
            or parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID"))
        ),
        area=pick("area", "IM_AREA"),
        proxy=pick("proxy", "PROXY_URL") or None,
        data_dir=data_dir,
        watchlist_path=Path(os.getenv("IM_WATCHLIST") or ROOT / "watchlist.json"),
        headless=os.getenv("IM_HEADLESS", "1") != "0",
        dev_mode=os.getenv("IM_WEB_DEV", "0") == "1",
        poll_minutes=max(1, _int(over.get("poll_minutes"), DEFAULT_POLL_MINUTES)),
        cooldown_hours=max(
            0.0, _float(over.get("cooldown_hours"), DEFAULT_COOLDOWN_HOURS)
        ),
        poll_enabled=bool(over.get("poll_enabled", False)),
        build_version=pick("build_version", "IM_BUILD_VERSION") or BUILD_VERSION,
        transport=(
            pick("transport", "IM_TRANSPORT").lower()
            if pick("transport", "IM_TRANSPORT").lower() in TRANSPORTS
            else DEFAULT_TRANSPORT
        ),
        bootstrap_seconds=max(
            5,
            min(
                180,
                _int(
                    over.get("bootstrap_seconds")
                    or os.getenv("IM_BOOTSTRAP_SECONDS"),
                    DEFAULT_BOOTSTRAP_SECONDS,
                ),
            ),
        ),
    )
