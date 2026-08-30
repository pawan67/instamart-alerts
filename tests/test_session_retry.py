"""Session reuse and the blocked-poll retry ladder."""

from __future__ import annotations

import httpx
import pytest

from instamart_alerts import runner
from instamart_alerts.config import Settings
from instamart_alerts.session import (
    Blocked,
    SessionData,
    build_client,
    load_cached,
    save_cached,
    sync_cookies,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        bot_token="t",
        chat_id="c",
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
    )


def _response(client: httpx.Client, *set_cookie: str) -> httpx.Response:
    """A response as if www.swiggy.com had answered, so the jar absorbs it."""
    return httpx.Response(
        200,
        headers=[("set-cookie", v) for v in set_cookie],
        request=httpx.Request("GET", "https://www.swiggy.com/api/instamart/x"),
    )


def test_rotated_token_replaces_the_minted_one(tmp_path):
    data = SessionData(cookies={"aws-waf-token": "old", "deviceId": "d"}, device_id="d")
    client = build_client(_settings(tmp_path), data)

    client.cookies.extract_cookies(
        _response(client, "aws-waf-token=new; Domain=.swiggy.com; Path=/")
    )

    assert sync_cookies(client, data) is True
    assert data.cookies["aws-waf-token"] == "new"
    # The seeded cookie must be replaced, not shadowed by a second entry.
    assert [c.value for c in client.cookies.jar if c.name == "aws-waf-token"] == ["new"]
    # Cookies the server did not touch survive.
    assert data.cookies["deviceId"] == "d"


def test_sync_cookies_is_a_noop_when_nothing_rotated(tmp_path):
    data = SessionData(cookies={"aws-waf-token": "same"}, device_id="d")
    client = build_client(_settings(tmp_path), data)
    assert sync_cookies(client, data) is False


def test_remint_keeps_the_store_lookup(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    save_cached(
        settings,
        SessionData(
            cookies={"aws-waf-token": "stale"},
            device_id="old-device",
            store_id="1404876",
            area_label="401209",
            lat=19.4,
            lng=72.8,
        ),
    )
    monkeypatch.setattr(
        runner,
        "mint_token",
        lambda s: SessionData(cookies={"aws-waf-token": "fresh"}, device_id="new-device"),
    )

    client, data = runner.open_session(settings, force_refresh=True)
    client.close()

    assert data.cookies["aws-waf-token"] == "fresh"
    assert data.device_id == "new-device"  # must match the new token
    assert (data.store_id, data.area_label) == ("1404876", "401209")
    assert (data.lat, data.lng) == (19.4, 72.8)
    assert load_cached(settings).store_id == "1404876"


def _stub_session(monkeypatch, tmp_path):
    """Make open_session cheap; return the list recording each call."""
    calls: list[bool] = []

    def fake_open(settings, *, force_refresh=False, previous=None):
        calls.append(force_refresh)
        data = SessionData(cookies={"aws-waf-token": f"t{len(calls)}"}, device_id="d")
        return build_client(_settings(tmp_path), data), data

    monkeypatch.setattr(runner, "open_session", fake_open)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    return calls


def test_retries_until_a_token_sticks(tmp_path, monkeypatch):
    calls = _stub_session(monkeypatch, tmp_path)
    attempts = []

    def fake_run(settings, watchlist, client, data, dry_run, cooldown_hours):
        attempts.append(data.cookies["aws-waf-token"])
        if len(attempts) < 3:
            raise Blocked("GET /maps/suggestions -> HTTP 202")
        return ["ok"]

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    assert attempts == ["t1", "t2", "t3"]
    assert calls == [False, True, True]  # only the retries force a re-mint


def test_gives_up_after_the_ladder_and_reraises(tmp_path, monkeypatch):
    _stub_session(monkeypatch, tmp_path)

    def always_blocked(*a, **kw):
        raise Blocked("POST /search/v2 -> HTTP 202")

    monkeypatch.setattr(runner, "_run", always_blocked)

    with pytest.raises(Blocked, match="search/v2"):
        runner.run_once(_settings(tmp_path), object())


def test_a_failed_mint_is_retried_too(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    calls: list[bool] = []

    def flaky_open(settings, *, force_refresh=False, previous=None):
        calls.append(force_refresh)
        if len(calls) == 1:
            raise Blocked("browser bootstrap finished without an aws-waf-token")
        data = SessionData(cookies={"aws-waf-token": "good"}, device_id="d")
        return build_client(_settings(tmp_path), data), data

    monkeypatch.setattr(runner, "open_session", flaky_open)
    monkeypatch.setattr(runner, "_run", lambda *a: ["ok"])

    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    assert calls == [False, True]


def test_connection_failure_retries_without_re_minting(tmp_path, monkeypatch):
    calls = _stub_session(monkeypatch, tmp_path)
    attempts = []

    def fake_run(settings, watchlist, client, data, dry_run, cooldown_hours):
        attempts.append(data.cookies["aws-waf-token"])
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("_ssl.c:1015: The handshake operation timed out")
        return ["ok"]

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    # A dead tunnel says nothing about the token, so the session is reused and
    # no second browser bootstrap is paid for.
    assert attempts == ["t1", "t1"]
    assert calls == [False]


def test_connection_failure_gives_up_after_the_ladder(tmp_path, monkeypatch):
    _stub_session(monkeypatch, tmp_path)

    def always_broken(*a, **kw):
        raise httpx.ConnectError("[SSL: UNEXPECTED_MESSAGE] unexpected message")

    monkeypatch.setattr(runner, "_run", always_broken)

    with pytest.raises(httpx.ConnectError, match="UNEXPECTED_MESSAGE"):
        runner.run_once(_settings(tmp_path), object())


def test_a_block_then_a_connection_failure_both_recover(tmp_path, monkeypatch):
    calls = _stub_session(monkeypatch, tmp_path)
    attempts = []

    def fake_run(settings, watchlist, client, data, dry_run, cooldown_hours):
        attempts.append(data.cookies["aws-waf-token"])
        if len(attempts) == 1:
            raise Blocked("GET /maps/suggestions -> HTTP 202")
        if len(attempts) == 2:
            raise httpx.ConnectTimeout("handshake timed out")
        return ["ok"]

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    # Re-mint after the block, then reuse that same token after the timeout.
    assert attempts == ["t1", "t2", "t2"]
    assert calls == [False, True]
