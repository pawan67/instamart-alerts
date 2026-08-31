"""Deciding when a WAF bootstrap has actually worked.

The old test was "is there an aws-waf-token?", which is true one beat before the
challenge reloads the page — and a token taken at that moment is answered 202 on
its first use. These pin the real condition: the token was accepted.
"""

from __future__ import annotations

import pytest

from instamart_alerts.session import _cleared, _clear_challenge, _diagnose

TOKEN = {"name": "aws-waf-token", "value": "t"}
SITE = [TOKEN] + [{"name": f"c{i}", "value": "v"} for i in range(14)]


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    """Counts goto() calls and hands back a scripted status for each."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.gotos = 0
        self.waited = 0

    def goto(self, _url, **_kw):
        self.gotos += 1
        return FakeResponse(self.statuses.pop(0) if self.statuses else 200)

    def wait_for_timeout(self, ms):
        self.waited += ms


class FakeCtx:
    """Serves a scripted sequence of cookie jars, holding the last one."""

    def __init__(self, jars):
        self.jars = list(jars)

    def cookies(self):
        return self.jars[0] if len(self.jars) == 1 else self.jars.pop(0)


# ── the success condition ────────────────────────────────────────────
def test_a_lone_token_is_not_a_cleared_session():
    assert _cleared({"aws-waf-token": "t"}) is False


def test_a_token_plus_the_sites_own_cookies_is():
    assert _cleared({"aws-waf-token": "t", "deviceId": "d"}) is True


def test_site_cookies_without_a_token_are_not_enough():
    assert _cleared({"deviceId": "d"}) is False


# ── the clearance loop ───────────────────────────────────────────────
def test_it_reloads_once_the_token_appears_and_accepts_a_200():
    page = FakePage([200])
    ctx = FakeCtx([[TOKEN], SITE])
    cookies, status = _clear_challenge(page, ctx, seconds=5)
    assert status == 200
    assert len(cookies) == 15
    assert page.gotos == 1


def test_a_202_on_the_reload_keeps_waiting_rather_than_declaring_success():
    """This is the bug: 202 + one cookie used to be reported as success."""
    page = FakePage([202, 202, 200])
    ctx = FakeCtx([[TOKEN], [TOKEN], [TOKEN], [TOKEN], SITE])
    cookies, status = _clear_challenge(page, ctx, seconds=5)
    assert status == 200
    assert _cleared(cookies)
    assert page.gotos == 3


def test_it_gives_up_at_the_deadline_and_reports_what_it_had():
    page = FakePage([202] * 50)
    ctx = FakeCtx([[TOKEN]])
    cookies, status = _clear_challenge(page, ctx, seconds=0)
    assert status == 202
    assert not _cleared(cookies)


def test_no_token_means_no_reload_is_attempted():
    page = FakePage([])
    ctx = FakeCtx([[]])
    cookies, status = _clear_challenge(page, ctx, seconds=0)
    assert page.gotos == 0
    assert status is None
    assert cookies == {}


# ── and it says so in a way that points somewhere ────────────────────
def test_a_lone_token_is_diagnosed_as_an_unvalidated_challenge():
    why = _diagnose("<html>whatever</html>", {"aws-waf-token": "t"})
    assert "never validated" in why
    assert "sticky" in why

