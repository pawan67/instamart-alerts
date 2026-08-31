"""Deciding when a WAF bootstrap has actually worked, and how hard to push.

Two bugs live here, one behind the other. The first was "is there an
aws-waf-token?", which is true one beat before the challenge reloads the page —
and a token taken at that moment is answered 202 on its first use. Reloading to
check for that turned out to be the second: a `goto` cancels the challenge round
that is running, the WAF only tolerates a few cancelled rounds, and a loop that
reloaded once a second walked straight into "max challenge attempts exceeded".

So these pin both halves: the token was accepted, *and* we were patient getting
there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from instamart_alerts import session
from instamart_alerts.session import (
    MAX_RELOADS,
    SELF_RELOAD_GRACE,
    _cleared,
    _clear_challenge,
    _diagnose,
)

TOKEN = {"name": "aws-waf-token", "value": "t"}
SITE = [TOKEN] + [{"name": f"c{i}", "value": "v"} for i in range(14)]

EXHAUSTED_PAGE = (
    "<html><body>Max challenge attempts exceeded. "
    "Please refresh the page to try again!</body></html>"
)


class Clock:
    """Monotonic time the test drives, so grace periods cost no wall clock."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(session, "time", c)
    return c


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeNavigation:
    """What the response listener sees when the page navigates by itself."""

    def __init__(self, status, url="https://www.swiggy.com/instamart"):
        self.status = status
        self.url = url
        self.request = SimpleNamespace(resource_type="document")


class FakePage:
    """Counts goto() calls, advances the clock, and can navigate on its own.

    `tick` is called on every poll, which is how a test stands in for the
    challenge script finishing and reloading the page without being asked to.
    """

    def __init__(self, clock, statuses=(), *, tick=None, content=""):
        self.clock = clock
        self.statuses = list(statuses)
        self.gotos = 0
        self.ticks = 0
        self._listeners = []
        self._tick = tick
        self._content = content

    # ── the Playwright surface `_clear_challenge` uses ──
    def on(self, event, fn):
        assert event == "response"
        self._listeners.append(fn)

    def remove_listener(self, event, fn):
        self._listeners.remove(fn)

    def content(self):
        return self._content

    def goto(self, _url, **_kw):
        self.gotos += 1
        return FakeResponse(self.statuses.pop(0) if self.statuses else 200)

    def wait_for_timeout(self, ms):
        self.ticks += 1
        self.clock.advance(ms / 1000)
        if self._tick:
            self._tick(self, self.ticks)

    # ── what the challenge script does for itself ──
    def navigate(self, status):
        for fn in list(self._listeners):
            fn(FakeNavigation(status))


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


# ── patience: the challenge reloads itself, and that has to be enough ──
def test_a_challenge_that_clears_itself_is_never_reloaded_over(clock):
    """The happy path costs zero goto()s — the script does its own reload."""

    def tick(page, n):
        if n == 2:
            page.navigate(200)

    page = FakePage(clock, tick=tick)
    ctx = FakeCtx([[TOKEN], [TOKEN], SITE])

    cookies, status = _clear_challenge(page, ctx, seconds=30, status=202)

    assert page.gotos == 0
    assert status == 200
    assert _cleared(cookies)


def test_a_self_reload_that_comes_back_202_is_not_mistaken_for_success(clock):
    """Only a 200 clears. A token plus stale site cookies must not fool it."""

    def tick(page, n):
        if n == 1:
            page.navigate(202)

    page = FakePage(clock, [202] * 10, tick=tick)
    ctx = FakeCtx([SITE])  # token *and* cookies, but the WAF still says 202

    cookies, status = _clear_challenge(page, ctx, seconds=30, status=202)

    assert status == 202
    assert not (status == 200 and _cleared(cookies))


def test_a_stalled_challenge_is_reloaded_but_only_after_the_grace(clock):
    page = FakePage(clock, [200])
    ctx = FakeCtx([[TOKEN], SITE])

    cookies, status = _clear_challenge(page, ctx, seconds=30, status=202)

    # Nothing happened for the whole grace period before we stepped in.
    assert page.ticks == pytest.approx(SELF_RELOAD_GRACE / 0.5)
    assert page.gotos == 1
    assert status == 200
    assert _cleared(cookies)


# ── and the storm that took the poller down ──────────────────────────
def test_a_challenge_that_never_clears_is_not_reloaded_into_the_ground(clock):
    """The outage: goto() once a second cancelled each fresh challenge round
    mid-flight, ~25 times a bootstrap, until the WAF stopped serving them."""
    page = FakePage(clock, [202] * 200)
    ctx = FakeCtx([[TOKEN]])

    cookies, status = _clear_challenge(page, ctx, seconds=120, status=202)

    assert page.gotos == MAX_RELOADS
    assert status == 202
    assert not _cleared(cookies)


def test_the_max_attempts_page_stops_us_reloading_at_all(clock):
    """Once the WAF says it is done, another reload only digs deeper."""
    page = FakePage(clock, [202] * 10, content=EXHAUSTED_PAGE)
    ctx = FakeCtx([[TOKEN]])

    cookies, _ = _clear_challenge(page, ctx, seconds=60, status=202)

    assert page.gotos == 0
    assert not _cleared(cookies)


def test_no_token_means_no_reload_is_attempted(clock):
    """Without a token there is nothing to validate, and a reload just spends
    another challenge attempt restarting a round from scratch."""
    page = FakePage(clock)
    ctx = FakeCtx([[]])

    cookies, status = _clear_challenge(page, ctx, seconds=30, status=202)

    assert page.gotos == 0
    assert status == 202
    assert cookies == {}


# ── and it says so in a way that points somewhere ────────────────────
def test_a_lone_token_is_diagnosed_as_an_unvalidated_challenge():
    why = _diagnose("<html>whatever</html>", {"aws-waf-token": "t"})
    assert "never validated" in why
    assert "sticky" in why


def test_the_max_attempts_page_is_diagnosed_as_exhaustion():
    """It sets only aws-waf-token, so the lone-token branch used to claim it
    first and blame the proxy for a wall we had walked into ourselves."""
    why = _diagnose(EXHAUSTED_PAGE, {"aws-waf-token": "t"})
    assert "stopped answering challenges" in why


def test_a_rotating_proxy_username_is_flagged():
    settings = SimpleNamespace(proxy="http://user:pw@gw.dataimpulse.com:823")
    assert session._warn_if_rotating(settings) is None  # logs, does not raise


@pytest.mark.parametrize(
    "user, sticky",
    [
        ("user", False),
        ("user__cr.in", False),
        ("user__cr.in;sessid.instamart", True),
        ("brd-customer-x-session-abc", True),
    ],
)
def test_sticky_markers_recognise_a_pinned_session(user, sticky):
    proxy = f"http://{user}:pw@gw.dataimpulse.com:823"
    from urllib.parse import urlparse

    name = (urlparse(proxy).username or "").lower()
    assert any(m in name for m in session.STICKY_MARKERS) is sticky


# ── the proxy scheme the browser can actually use ────────────────────
def test_an_authenticated_socks5_proxy_is_refused_before_playwright_sees_it():
    """Chromium cannot authenticate to SOCKS5, and it is the browser that mints
    the token — so this fails in the one place a bad proxy hurts most."""
    settings = SimpleNamespace(
        headless=True, proxy="socks5://user:pw@gw.dataimpulse.com:10000"
    )
    with pytest.raises(session.Blocked, match="http://"):
        session._launch_options(settings)


def test_the_same_gateway_over_http_is_accepted_sticky_username_and_all():
    settings = SimpleNamespace(
        headless=True,
        proxy="http://user__cr.in;sessid.instamart;sessttl.60:pw@gw.dataimpulse.com:10000",
    )
    proxy = session._launch_options(settings)["proxy"]
    assert proxy["server"] == "http://gw.dataimpulse.com:10000"
    assert proxy["username"] == "user__cr.in;sessid.instamart;sessttl.60"


# ── bandwidth: a headless poller renders nothing ─────────────────────
class FakeRoute:
    def __init__(self, kind):
        self.request = SimpleNamespace(resource_type=kind)
        self.verdict = None

    def abort(self):
        self.verdict = "abort"

    def continue_(self):
        self.verdict = "continue"


class RecordingCtx:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler


@pytest.mark.parametrize(
    "kind, verdict",
    [
        ("image", "abort"),      # product thumbnails: the bulk of the page
        ("media", "abort"),
        ("font", "abort"),
        ("script", "continue"),  # the challenge *is* a script
        ("stylesheet", "continue"),
        ("document", "continue"),
        ("xhr", "continue"),
    ],
)
def test_only_cosmetic_resources_are_dropped(kind, verdict):
    ctx = RecordingCtx()
    session._block_cosmetics(ctx)
    route = FakeRoute(kind)
    ctx.handler(route)
    assert route.verdict == verdict


# ── the browser profile, which is opt-in for a measured reason ───────
def test_the_browser_profile_is_off_by_default():
    """Caching `challenge.js` was the point and it was measured not to happen,
    so this stays opt-in rather than costing reliability for nothing."""
    from instamart_alerts import config

    assert config.Settings.browser_profile is False


def test_a_locked_profile_falls_back_to_a_cold_browser_rather_than_failing(tmp_path):
    """Two Chromiums cannot share a profile. A second caller — the CLI while the
    panel polls — should lose the cache, not the run."""

    class Boom:
        def launch_persistent_context(self, *_a, **_k):
            raise RuntimeError("profile is already in use")

        def launch(self, **_k):
            return SimpleNamespace(
                new_context=lambda **_: SimpleNamespace(new_page=lambda: "page")
            )

    settings = SimpleNamespace(
        browser_profile=True, data_dir=tmp_path, headless=True, proxy=None
    )
    ctx, page = session._open_context(SimpleNamespace(chromium=Boom()), settings)
    assert page == "page"
