"""Pinning the exit IP, and rotating it when the WAF says no.

The WAF issues its token to an address, not to a cookie jar. Three things have
to leave from that one address for a poll to work — the challenge, the reload
that validates the token, and every later call carrying it — and on a rotating
residential gateway none of them do unless the username asks. These pin the
mechanism: where the id comes from, who reuses it, and who insists on a new one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from instamart_alerts import runner, session
from instamart_alerts.config import Settings
from instamart_alerts.session import (
    SessionData,
    _diagnose,
    _warn_if_rotating,
    new_session_id,
    proxy_url,
)

GATEWAY = "gw.dataimpulse.com:823"


def make(proxy: str | None, tmp_path=None) -> Settings:
    return Settings(
        bot_token="t",
        chat_id="c",
        area="401209",
        proxy=proxy,
        data_dir=tmp_path or Path("."),
        watchlist_path=Path("watchlist.json"),
        headless=True,
    )


# ── filling the placeholder in ───────────────────────────────────────
def test_no_proxy_stays_no_proxy():
    assert proxy_url(make(None), "abc") is None


def test_a_url_without_the_placeholder_is_handed_back_untouched():
    url = f"http://user:pw@{GATEWAY}"
    assert proxy_url(make(url), "abc") == url


def test_the_placeholder_is_filled_in_wherever_the_provider_wants_it():
    url = f"http://user__cr.in;sessid.{{session}};sessttl.30:pw@{GATEWAY}"
    assert proxy_url(make(url), "abc123") == (
        f"http://user__cr.in;sessid.abc123;sessttl.30:pw@{GATEWAY}"
    )


@pytest.mark.parametrize(
    "user",
    [
        "brd-customer-c1-zone-z1-session-{session}",  # Bright Data
        "customer-c1-sessid-{session}-sesstime-30",  # Oxylabs
        "user-sp1-session-{session}",  # Smartproxy
    ],
)
def test_other_providers_spell_it_differently_and_all_work(user):
    filled = proxy_url(make(f"http://{user}:pw@{GATEWAY}"), "zz")
    assert "{session}" not in filled
    assert "-zz" in filled or "sessid-zz" in filled


def test_an_empty_id_still_yields_a_usable_url():
    """Better a random exit than a literal '{session}' handed to the gateway."""
    filled = proxy_url(make(f"http://u;sessid.{{session}}:pw@{GATEWAY}"), "")
    assert "{session}" not in filled


def test_ids_do_not_repeat():
    assert len({new_session_id() for _ in range(50)}) == 50


# ── who reuses an id, and who insists on a new one ───────────────────
def test_the_id_rides_on_the_session_so_polls_land_on_the_minting_exit():
    data = SessionData(cookies={"aws-waf-token": "t"}, proxy_session="sid-1")
    round_tripped = SessionData.from_json(json.loads(json.dumps(data.to_json())))
    assert round_tripped.proxy_session == "sid-1"


def test_an_old_cache_without_an_id_loads_rather_than_explodes():
    assert SessionData.from_json({"cookies": {}}).proxy_session == ""


def test_build_client_dials_the_exit_the_token_was_minted_behind(tmp_path, monkeypatch):
    seen = {}
    real = session.httpx.Client

    def spy(*a, **kw):
        seen["proxy"] = kw.get("proxy")
        return real(*a, **kw)

    monkeypatch.setattr(session.httpx, "Client", spy)
    data = SessionData(cookies={"aws-waf-token": "t"}, proxy_session="sid-7")
    url = f"http://u;sessid.{{session}}:pw@{GATEWAY}"
    session.build_client(make(url, tmp_path), data).close()
    assert seen["proxy"] == f"http://u;sessid.sid-7:pw@{GATEWAY}"


def test_a_cached_token_is_replayed_from_the_exit_that_earned_it(
    tmp_path, monkeypatch
):
    """The one that decides whether a poll costs three seconds or thirty: a
    token replayed from a new address is refused, and pays for a bootstrap."""
    seen = {}
    real = session.httpx.Client
    monkeypatch.setattr(
        session.httpx,
        "Client",
        lambda *a, **kw: (seen.update(proxy=kw.get("proxy")), real(*a, **kw))[1],
    )
    settings = make(f"http://u;sessid.{{session}}:pw@{GATEWAY}", tmp_path)
    session.save_cached(
        settings, SessionData(cookies={"aws-waf-token": "t"}, proxy_session="sid-3")
    )

    client, data = runner.open_session(settings)
    client.close()

    assert data.proxy_session == "sid-3"
    assert seen["proxy"] == f"http://u;sessid.sid-3:pw@{GATEWAY}"


def test_a_remint_asks_for_an_exit_the_last_one_was_not_refused_at(
    tmp_path, monkeypatch
):
    """The whole point of the retry ladder. Re-minting behind the address that
    just refused a token is asking the same question twice."""
    asked: list[str] = []

    def fake_mint(settings, session_id=None):
        asked.append(session_id)
        return SessionData(cookies={"aws-waf-token": "t"}, proxy_session=session_id)

    monkeypatch.setattr(runner, "mint_token", fake_mint)
    settings = make(f"http://u;sessid.{{session}}:pw@{GATEWAY}", tmp_path)
    for _ in range(3):
        client, _ = runner.open_session(settings, force_refresh=True)
        client.close()

    assert all(asked) and len(set(asked)) == 3


def test_the_browser_transport_keeps_the_id_it_was_given():
    """A cached token means a live exit to stay next to, not one to leave."""
    data = SessionData(cookies={"aws-waf-token": "t"}, proxy_session="sid-9")
    url = f"http://u;sessid.{{session}}:pw@{GATEWAY}"
    client = session.BrowserClient(make(url), data)
    assert client._data.proxy_session == "sid-9"


# ── saying so ────────────────────────────────────────────────────────
def test_a_username_with_nowhere_to_put_an_id_is_called_out(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_rotating(make(f"http://user__cr.in:pw@{GATEWAY}"))
    assert "sticky-session" in caplog.text
    assert "{session}" in caplog.text


def test_a_hardcoded_id_gets_its_own_advice(caplog):
    """It is sticky, which is half the problem solved and the other half pinned
    open: every retry leaves from the address the last one was refused at."""
    with caplog.at_level(logging.WARNING):
        _warn_if_rotating(make(f"http://user;sessid.instamart:pw@{GATEWAY}"))
    assert "fixed session" in caplog.text


def test_a_placeholder_is_left_in_peace(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_rotating(make(f"http://user;sessid.{{session}}:pw@{GATEWAY}"))
    assert caplog.text == ""


TOKEN_ONLY = {"aws-waf-token": "t"}


def test_a_refusal_from_an_address_that_held_still_blames_the_address():
    why = _diagnose("<html>challenge.js</html>", TOKEN_ONLY, ip_held=True)
    assert "distrusted" in why
    assert "sticky session" not in why  # the one thing it is not


def test_a_refusal_after_the_address_moved_still_blames_the_proxy():
    why = _diagnose("<html>challenge.js</html>", TOKEN_ONLY, ip_held=False)
    assert "sticky session" in why


def test_an_unreadable_probe_falls_back_to_the_likelier_cause():
    assert "sticky session" in _diagnose("<html></html>", TOKEN_ONLY, ip_held=None)
