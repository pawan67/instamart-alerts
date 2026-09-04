"""Session reuse and the blocked-poll retry ladder."""

from __future__ import annotations

import httpx
import pytest

from instamart_alerts import runner
from instamart_alerts.config import API, Settings
from instamart_alerts.session import (
    Blocked,
    SessionData,
    build_client,
    load_cached,
    request,
    save_cached,
    sync_cookies,
)


def _settings(tmp_path, *, transport: str = "http") -> Settings:
    return Settings(
        bot_token="t",
        chat_id="c",
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
        transport=transport,
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
        lambda s, sid=None: SessionData(
            cookies={"aws-waf-token": "fresh"}, device_id="new-device"
        ),
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
    transports: list[str] = []

    def fake_open(settings, *, force_refresh=False, previous=None, browser=False):
        calls.append(force_refresh)
        transports.append("browser" if browser else "http")
        data = SessionData(cookies={"aws-waf-token": f"t{len(calls)}"}, device_id="d")
        return build_client(_settings(tmp_path), data), data

    monkeypatch.setattr(runner, "open_session", fake_open)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    return calls, transports


def test_retries_until_a_token_sticks(tmp_path, monkeypatch):
    calls, transports = _stub_session(monkeypatch, tmp_path)
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
    calls, transports = _stub_session(monkeypatch, tmp_path)

    def always_blocked(*a, **kw):
        raise Blocked("POST /search/v2 -> HTTP 202")

    monkeypatch.setattr(runner, "_run", always_blocked)

    with pytest.raises(Blocked, match="search/v2"):
        runner.run_once(_settings(tmp_path), object())


def test_a_failed_mint_is_retried_too(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    calls: list[bool] = []

    def flaky_open(settings, *, force_refresh=False, previous=None, browser=False):
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
    calls, transports = _stub_session(monkeypatch, tmp_path)
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
    calls, transports = _stub_session(monkeypatch, tmp_path)

    def always_broken(*a, **kw):
        raise httpx.ConnectError("[SSL: UNEXPECTED_MESSAGE] unexpected message")

    monkeypatch.setattr(runner, "_run", always_broken)

    with pytest.raises(httpx.ConnectError, match="UNEXPECTED_MESSAGE"):
        runner.run_once(_settings(tmp_path), object())


def test_a_block_then_a_connection_failure_both_recover(tmp_path, monkeypatch):
    calls, transports = _stub_session(monkeypatch, tmp_path)
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


def _mock_client(status: int, body: bytes, content_type: str | None) -> httpx.Client:
    headers = {} if content_type is None else {"content-type": content_type}
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, content=body, headers=headers)
        )
    )


CHALLENGE_PAGE = b"<!DOCTYPE html><html><head><title>Just a moment</title>"


def test_a_challenge_served_as_200_html_counts_as_blocked():
    """The WAF does not always use 202 — sometimes it just returns the page."""
    client = _mock_client(200, CHALLENGE_PAGE, "text/html; charset=utf-8")
    with pytest.raises(Blocked, match="not JSON"):
        request(client, "POST", f"{API}/search/v2")


def test_an_empty_body_counts_as_blocked():
    client = _mock_client(200, b"", "application/json")
    with pytest.raises(Blocked, match="not JSON"):
        request(client, "GET", f"{API}/maps/suggestions")


def test_a_missing_content_type_counts_as_blocked():
    client = _mock_client(200, b'{"data": []}', None)
    with pytest.raises(Blocked, match="missing"):
        request(client, "GET", f"{API}/maps/suggestions")


def test_real_json_passes_through():
    client = _mock_client(200, b'{"data": [1]}', "application/json")
    assert request(client, "GET", f"{API}/maps/suggestions").json() == {"data": [1]}


def test_202_still_blocks_before_the_body_is_examined():
    client = _mock_client(202, b'{"data": []}', "application/json")
    with pytest.raises(Blocked, match="HTTP 202"):
        request(client, "GET", f"{API}/maps/suggestions")


def test_a_server_error_is_not_mistaken_for_a_challenge():
    client = _mock_client(500, CHALLENGE_PAGE, "text/html")
    with pytest.raises(httpx.HTTPStatusError):
        request(client, "GET", f"{API}/maps/suggestions")


# ── transport escalation ─────────────────────────────────────────────
def test_auto_spends_the_cheap_attempts_before_the_browser(tmp_path, monkeypatch):
    """A Chromium per pass is expensive; it is the last roll, not the first."""
    calls, transports = _stub_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner, "_run", lambda *a: (_ for _ in ()).throw(Blocked("HTTP 202"))
    )
    with pytest.raises(Blocked):
        runner.run_once(_settings(tmp_path, transport="auto"), object())
    assert transports == ["http", "http", "browser"]


def test_browser_mode_skips_httpx_entirely(tmp_path, monkeypatch):
    calls, transports = _stub_session(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_run", lambda *a: ["ok"])
    runner.run_once(_settings(tmp_path, transport="browser"), object())
    assert transports == ["browser"]


def test_http_mode_never_reaches_for_the_browser(tmp_path, monkeypatch):
    calls, transports = _stub_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner, "_run", lambda *a: (_ for _ in ()).throw(Blocked("HTTP 202"))
    )
    with pytest.raises(Blocked):
        runner.run_once(_settings(tmp_path, transport="http"), object())
    assert transports == ["http", "http", "http"]


def test_auto_does_not_pay_for_a_browser_when_the_first_try_works(tmp_path, monkeypatch):
    calls, transports = _stub_session(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_run", lambda *a: ["ok"])
    runner.run_once(_settings(tmp_path, transport="auto"), object())
    assert transports == ["http"]


@pytest.mark.parametrize(
    "transport, attempt, expected",
    [
        ("auto", 1, False),
        ("auto", 2, False),
        ("auto", 3, True),
        ("http", 3, False),
        ("browser", 1, True),
    ],
)
def test_use_browser_on(tmp_path, transport, attempt, expected):
    s = _settings(tmp_path, transport=transport)
    assert runner.use_browser_on(attempt, s) is expected


# ── proxy failures ───────────────────────────────────────────────────
def test_a_socks_handshake_failure_is_retried_not_fatal(tmp_path, monkeypatch):
    """socksio raises past httpx's mapping, so it used to kill the whole pass."""
    from socksio.exceptions import ProtocolError

    calls, transports = _stub_session(monkeypatch, tmp_path)
    attempts = []

    def flaky(settings, watchlist, client, data, dry_run, cooldown_hours):
        attempts.append(1)
        if len(attempts) < 2:
            raise ProtocolError("Malformed reply")
        return ["ok"]

    monkeypatch.setattr(runner, "_run", flaky)
    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    assert len(attempts) == 2


def test_a_socks_failure_that_never_clears_still_reraises(tmp_path, monkeypatch):
    from socksio.exceptions import ProtocolError

    _stub_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner, "_run", lambda *a: (_ for _ in ()).throw(ProtocolError("Malformed reply"))
    )
    with pytest.raises(ProtocolError):
        runner.run_once(_settings(tmp_path), object())


def test_a_dead_tunnel_keeps_the_session_rather_than_re_minting(tmp_path, monkeypatch):
    """A transport error means Swiggy never saw us; the token is still good."""
    calls, transports = _stub_session(monkeypatch, tmp_path)
    attempts = []

    def flaky(settings, watchlist, client, data, dry_run, cooldown_hours):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("tunnel died")
        return ["ok"]

    monkeypatch.setattr(runner, "_run", flaky)
    assert runner.run_once(_settings(tmp_path), object()) == ["ok"]
    # Only one open_session: the client was reused, not rebuilt.
    assert calls == [False]
