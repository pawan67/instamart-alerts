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
from datetime import datetime, timedelta, timezone
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

# India has never observed daylight saving, so IST is a fixed offset. Spelling it
# out beats ZoneInfo("Asia/Kolkata") here: a slim container often ships no tzdata,
# and the failure would land at 3am inside the poll loop.
IST = timezone(timedelta(hours=5, minutes=30))

# Nothing worth being woken for is discounted overnight, and every poll costs
# metered proxy bandwidth. Hours are IST, on a 24h clock, end-exclusive.
DEFAULT_QUIET_START = 0  # midnight
DEFAULT_QUIET_END = 6


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
    # Skip images/media/fonts in the bootstrap browser. Nothing here renders, so
    # this is free bandwidth — set IM_BLOCK_IMAGES=0 if the WAF ever starts
    # minding a browser that loads no pictures.
    block_images: bool = True
    # Reuse one Chromium profile between bootstraps, so its disk cache survives.
    # Off by default: the hoped-for win was caching `challenge.js` (~300 KB a
    # round) and that was measured not to happen — Chromium re-fetches it every
    # time despite a stable URL and `max-age=86400`. What is left is Swiggy's own
    # SPA bundles, which only load on the browser transport and are plausibly but
    # unmeasurably cacheable. IM_BROWSER_PROFILE=1 to try it.
    browser_profile: bool = False
    # Skip *scheduled* passes overnight. Manual checks from the panel always run
    # — asking for one is unambiguous, whatever the clock says.
    quiet_hours: bool = True
    quiet_start: int = DEFAULT_QUIET_START
    quiet_end: int = DEFAULT_QUIET_END

    @property
    def chat_ids(self) -> tuple[str, ...]:
        return parse_chat_ids(self.chat_id)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_ids)


def in_quiet_hours(settings: Settings, now: datetime | None = None) -> bool:
    """True when the poller should stay asleep.

    The window is allowed to wrap midnight (22 -> 6 is four hours of evening and
    six of morning), and a zero-length window means "never", so setting both ends
    the same cannot lock the poller out for a whole day.
    """
    if not settings.quiet_hours or settings.quiet_start == settings.quiet_end:
        return False
    hour = (now or datetime.now(IST)).astimezone(IST).hour
    if settings.quiet_start < settings.quiet_end:
        return settings.quiet_start <= hour < settings.quiet_end
    return hour >= settings.quiet_start or hour < settings.quiet_end


def minutes_until_quiet_end(settings: Settings, now: datetime | None = None) -> float:
    """Minutes from now until the quiet window ends. 0 when not inside one.

    The poller uses this instead of its own interval so a quiet night is one
    sleep rather than a check every quarter of an hour that only logs that it
    is night.
    """
    if not in_quiet_hours(settings, now):
        return 0.0
    now = (now or datetime.now(IST)).astimezone(IST)
    end = now.replace(hour=settings.quiet_end, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return (end - now).total_seconds() / 60.0


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


def _hour(value, fallback: int) -> int:
    """An hour on a 24h clock, or the fallback. Out-of-range is a typo, not a wrap."""
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return fallback
    return hour if 0 <= hour <= 23 else fallback


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
        block_images=str(
            over.get("block_images", os.getenv("IM_BLOCK_IMAGES", "1"))
        ).lower() not in ("0", "false", "no"),
        browser_profile=str(
            over.get("browser_profile", os.getenv("IM_BROWSER_PROFILE", "0"))
        ).lower() in ("1", "true", "yes"),
        quiet_hours=str(
            over.get("quiet_hours", os.getenv("IM_QUIET_HOURS", "1"))
        ).lower() not in ("0", "false", "no"),
        quiet_start=_hour(
            over.get("quiet_start", os.getenv("IM_QUIET_START")), DEFAULT_QUIET_START
        ),
        quiet_end=_hour(
            over.get("quiet_end", os.getenv("IM_QUIET_END")), DEFAULT_QUIET_END
        ),
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
