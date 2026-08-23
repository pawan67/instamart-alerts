"""Swiggy session handling.

Instamart sits behind an AWS WAF JS challenge, so plain HTTP gets a 202 + a
challenge page. A headless Chromium solves the challenge once and hands us an
`aws-waf-token`; every later poll is a cheap httpx call carrying that cookie.
The token is cached on disk and only re-minted when Swiggy starts refusing us.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import API, BUILD_VERSION, USER_AGENT, Settings

log = logging.getLogger(__name__)

# A WAF challenge answer comes back as a 202 with an HTML body; a stale/absent
# token shows up as 403.
BLOCKED = (202, 403)


class Blocked(RuntimeError):
    """Swiggy refused the request — the WAF token needs re-minting."""


@dataclass
class SessionData:
    cookies: dict[str, str] = field(default_factory=dict)
    device_id: str = ""
    store_id: str = ""
    area_label: str = ""
    lat: float | None = None
    lng: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "cookies": self.cookies,
            "device_id": self.device_id,
            "store_id": self.store_id,
            "area_label": self.area_label,
            "lat": self.lat,
            "lng": self.lng,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SessionData:
        return cls(
            cookies=d.get("cookies") or {},
            device_id=d.get("device_id") or "",
            store_id=d.get("store_id") or "",
            area_label=d.get("area_label") or "",
            lat=d.get("lat"),
            lng=d.get("lng"),
        )


def _cache_path(settings: Settings) -> Path:
    return settings.data_dir / "session.json"


def load_cached(settings: Settings) -> SessionData | None:
    p = _cache_path(settings)
    if not p.exists():
        return None
    try:
        return SessionData.from_json(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("ignoring unreadable session cache: %s", e)
        return None


def save_cached(settings: Settings, data: SessionData) -> None:
    _cache_path(settings).write_text(json.dumps(data.to_json(), indent=2))


def mint_token(settings: Settings) -> SessionData:
    """Launch headless Chromium, clear the WAF challenge, keep the cookies."""
    from playwright.sync_api import sync_playwright  # imported lazily: slow

    log.info("minting a fresh WAF token via headless Chromium")
    launch: dict[str, Any] = {
        "headless": settings.headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if settings.proxy:
        import urllib.parse
        parsed = urllib.parse.urlparse(settings.proxy)
        if parsed.username and parsed.password:
            launch["proxy"] = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                "username": parsed.username,
                "password": parsed.password,
            }
        else:
            launch["proxy"] = {"server": settings.proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = ctx.new_page()
        page.goto(
            "https://www.swiggy.com/instamart",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        # The challenge script needs a moment to run and set the cookie.
        for _ in range(20):
            page.wait_for_timeout(1_000)
            if any(c["name"] == "aws-waf-token" for c in ctx.cookies()):
                break
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()

    if "aws-waf-token" not in cookies:
        raise Blocked("browser bootstrap finished without an aws-waf-token")

    # deviceId arrives signed as `s:<uuid>.<signature>`; the API wants the uuid.
    raw = urllib.parse.unquote(cookies.get("deviceId", ""))
    device_id = raw.removeprefix("s:").split(".")[0]
    return SessionData(cookies=cookies, device_id=device_id)


def build_client(settings: Settings, data: SessionData) -> httpx.Client:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-IN",
        "content-type": "application/json",
        "x-build-version": BUILD_VERSION,
        "x-device-id": data.device_id,
        "Origin": "https://www.swiggy.com",
        "Referer": "https://www.swiggy.com/instamart",
    }
    return httpx.Client(
        headers=headers,
        cookies=data.cookies,
        timeout=30.0,
        follow_redirects=True,
        proxy=settings.proxy,
    )


def request(client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    """Issue a call, converting WAF refusals into `Blocked`."""
    r = client.request(method, url, **kw)
    if r.status_code in BLOCKED:
        raise Blocked(f"{method} {url.removeprefix(API)} -> HTTP {r.status_code}")
    r.raise_for_status()
    return r
