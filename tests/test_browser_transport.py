"""The in-page transport, exercised against a fake Playwright page.

What matters is that it looks enough like an httpx client for `request()` and
everything above it, and that a call really is issued from the page rather than
from Python — the whole point is the fingerprint.
"""

from __future__ import annotations

import json

import httpx
import pytest

from instamart_alerts.config import Settings
from instamart_alerts.session import (
    Blocked,
    BrowserClient,
    BrowserResponse,
    SessionData,
    request,
)


def settings(tmp_path, **over) -> Settings:
    return Settings(
        bot_token="t",
        chat_id="c",
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
        **over,
    )


class FakePage:
    """Records what the page was asked to fetch and replays a canned answer."""

    def __init__(self, reply=None):
        self.seen: list[dict] = []
        self.reply = reply or {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "text": '{"data": "ok"}',
        }

    def evaluate(self, _js, arg):
        self.seen.append(arg)
        return self.reply


class FakeCookies:
    def __init__(self):
        self.added: list[dict] = []

    def refresh(self):
        pass


def client_on(tmp_path, page, **over) -> BrowserClient:
    c = BrowserClient(settings(tmp_path, **over), SessionData(device_id="dev-1"))
    c._page = page
    c.cookies = FakeCookies()
    return c


# ── the httpx-shaped surface ─────────────────────────────────────────
def test_a_get_carries_its_params_in_the_url(tmp_path):
    page = FakePage()
    c = client_on(tmp_path, page)
    c.request("GET", "https://x/api", params={"input": "401209 west"})
    assert page.seen[0]["url"] == "https://x/api?input=401209+west"
    assert page.seen[0]["body"] is None


def test_a_post_sends_a_json_body_and_content_type(tmp_path):
    page = FakePage()
    c = client_on(tmp_path, page)
    c.request("POST", "https://x/api", json={"query": "eggs"})
    sent = page.seen[0]
    assert json.loads(sent["body"]) == {"query": "eggs"}
    assert sent["headers"]["content-type"] == "application/json"


def test_the_swiggy_headers_ride_along(tmp_path):
    page = FakePage()
    c = client_on(tmp_path, page, build_version="9.9.9")
    c.request("GET", "https://x/api")
    assert page.seen[0]["headers"]["x-build-version"] == "9.9.9"
    assert page.seen[0]["headers"]["x-device-id"] == "dev-1"


def test_a_fetch_that_throws_looks_like_a_transport_error(tmp_path):
    """So the existing retry ladder redials instead of re-minting."""
    page = FakePage(reply={"error": "TypeError: Failed to fetch"})
    c = client_on(tmp_path, page)
    with pytest.raises(httpx.ConnectError, match="Failed to fetch"):
        c.request("GET", "https://x/api")


def test_calling_a_closed_client_is_an_error_not_a_hang(tmp_path):
    c = BrowserClient(settings(tmp_path), SessionData())
    with pytest.raises(RuntimeError, match="not open"):
        c.request("GET", "https://x/api")


# ── it must satisfy request()'s blocked-detection, unchanged ─────────
def test_request_accepts_a_browser_response(tmp_path):
    c = client_on(tmp_path, FakePage())
    assert request(c, "GET", "https://x/api").json() == {"data": "ok"}


def test_a_202_through_the_browser_is_still_blocked(tmp_path):
    page = FakePage(reply={"status": 202, "headers": {}, "text": "<html>"})
    with pytest.raises(Blocked, match="HTTP 202"):
        request(client_on(tmp_path, page), "GET", "https://x/api")


def test_an_html_body_through_the_browser_is_still_blocked(tmp_path):
    page = FakePage(
        reply={"status": 200, "headers": {"content-type": "text/html"}, "text": "<html>"}
    )
    with pytest.raises(Blocked, match="not JSON"):
        request(client_on(tmp_path, page), "GET", "https://x/api")


def test_an_empty_body_through_the_browser_is_still_blocked(tmp_path):
    page = FakePage(
        reply={"status": 200, "headers": {"content-type": "application/json"}, "text": " "}
    )
    with pytest.raises(Blocked, match="not JSON"):
        request(client_on(tmp_path, page), "GET", "https://x/api")


# ── the response shim ────────────────────────────────────────────────
def test_the_response_shim_behaves_like_httpx():
    r = BrowserResponse(200, {"content-type": "application/json"}, '{"a": 1}')
    assert r.json() == {"a": 1}
    assert r.content == b'{"a": 1}'
    assert r.raise_for_status() is r


def test_the_response_shim_raises_on_a_4xx():
    r = BrowserResponse(404, {}, "nope")
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()


def test_text_is_available_for_the_storeid_regex():
    """select-location is scraped with a regex over the raw body, not JSON."""
    r = BrowserResponse(200, {}, 'swiggy://store?storeId=1404876')
    assert "storeId=1404876" in r.text
