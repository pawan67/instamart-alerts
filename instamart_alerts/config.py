"""Environment-backed settings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# Swiggy rejects calls that look too far behind the deployed web build.
BUILD_VERSION = "2.367.0"
API = "https://www.swiggy.com/api/instamart"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: str
    area: str
    proxy: str | None
    data_dir: Path
    watchlist_path: Path
    headless: bool
    # Skips Mini App signature checks so the UI can be opened in a normal
    # browser. Never enable on anything reachable from the internet.
    dev_mode: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


def overrides_path(data_dir: Path) -> Path:
    """Runtime settings the web UI can change, layered over .env."""
    return data_dir / "settings.json"


def read_overrides(data_dir: Path) -> dict[str, str]:
    p = overrides_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_overrides(data_dir: Path, values: dict[str, str]) -> None:
    merged = read_overrides(data_dir) | values
    overrides_path(data_dir).write_text(json.dumps(merged, indent=2))


def load() -> Settings:
    data_dir = Path(os.getenv("IM_DATA_DIR") or ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    over = read_overrides(data_dir)
    return Settings(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        area=(over.get("area") or os.getenv("IM_AREA", "")).strip(),
        proxy=(os.getenv("PROXY_URL") or "").strip() or None,
        data_dir=data_dir,
        watchlist_path=Path(os.getenv("IM_WATCHLIST") or ROOT / "watchlist.json"),
        headless=os.getenv("IM_HEADLESS", "1") != "0",
        dev_mode=os.getenv("IM_WEB_DEV", "0") == "1",
    )
