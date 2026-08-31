"""Swiggy session handling.

Instamart sits behind an AWS WAF JS challenge, so plain HTTP gets a 202 + a
challenge page. A headless Chromium solves the challenge once and hands us an
`aws-waf-token`; every later poll is a cheap httpx call carrying that cookie.
The token is cached on disk and only re-minted when Swiggy starts refusing us.
"""

from __future__ import annotations

import json
import json as _json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import API, USER_AGENT, Settings

log = logging.getLogger(__name__)

# A WAF challenge answer comes back as a 202 with an HTML body; a stale/absent
# token shows up as 403.
BLOCKED = (202, 403)

# Every API call goes to www.swiggy.com, and the browser hands us cookies scoped
# to the parent domain, so we file them all under one key.
COOKIE_DOMAIN = ".swiggy.com"


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


# Markers in the page that say which wall we hit. The silent JS challenge and
# the interactive CAPTCHA look identical from the outside — no cookie — but only
# one of them can ever be solved by a headless browser, so they are worth
# telling apart before anyone spends an afternoon on browser flags.
CAPTCHA_MARKERS = ("captcha.awswaf.com", "captcha-container", "aws-waf-captcha")
CHALLENGE_MARKERS = ("challenge.js", "token.awswaf.com", "awswaf")

# The WAF's own interstitial once it has served more challenge rounds than it is
# willing to, none of them finished. It is a dead end: reloading past this page
# never works, it only deepens the hole.
EXHAUSTED_MARKERS = ("max challenge attempts exceeded",)


def _diagnose(html: str, cookies: dict[str, str]) -> str:
    """Say why no token arrived, in terms of what to do about it."""
    lowered = html.lower()
    if any(m in lowered for m in EXHAUSTED_MARKERS):
        return (
            "the WAF stopped answering challenges for this client — too many "
            "rounds were started and left unfinished. If it survives a restart "
            "the exit IP is rotating between the challenge and the reload, so "
            "every try begins a round nobody can finish: pin the IP with a "
            "sticky session"
        )
    if any(m in lowered for m in CAPTCHA_MARKERS):
        return (
            "Swiggy served an interactive CAPTCHA, which a headless browser "
            "cannot solve. This is what a datacenter IP usually gets — set a "
            "residential PROXY_URL (Connection, in the panel) and try again"
        )
    if "access denied" in lowered or "request blocked" in lowered:
        return (
            "the WAF refused the page outright. The IP is blocked rather than "
            "challenged; a different egress (PROXY_URL) is the only fix"
        )
    if list(cookies) == ["aws-waf-token"]:
        return (
            "the challenge handed over a token but never let the real page "
            "load, so the token was never validated. On a rotating proxy this "
            "is usually the exit IP changing between the challenge and the "
            "reload — use a sticky session"
        )
    if any(m in lowered for m in CHALLENGE_MARKERS):
        return (
            "the challenge script loaded but never finished. Give it longer "
            "(raise the bootstrap wait in the panel) — a small VPS can need "
            "well over 20s"
        )
    if not cookies:
        return (
            "the page set no cookies at all, so it was probably never reached "
            "— check egress, DNS and any proxy"
        )
    return (
        "the page loaded and set cookies, but no aws-waf-token among them "
        f"({', '.join(sorted(cookies)) or 'none'})"
    )


def _launch_options(settings: Settings) -> dict[str, Any]:
    """Chromium launch arguments, shared by minting and the browser transport."""
    args = ["--disable-blink-features=AutomationControlled"]
    if os.geteuid() == 0:
        # Chromium's sandbox needs privileges root does not get in a container.
        args += ["--no-sandbox", "--disable-dev-shm-usage"]

    launch: dict[str, Any] = {"headless": settings.headless, "args": args}
    if settings.proxy:
        parsed = urllib.parse.urlparse(settings.proxy)
        # Chromium's SOCKS5 client cannot authenticate, so a socks5:// URL with
        # credentials dies inside Playwright with a message about browser
        # support that says nothing about the proxy. httpx has no such limit,
        # which makes this fail in the one place that matters most — minting the
        # token — while every cheap poll carries on working.
        if parsed.scheme.startswith("socks") and parsed.username:
            raise Blocked(
                "Chromium cannot authenticate to a SOCKS5 proxy, and the WAF "
                "token can only be minted in a browser. Use the same gateway "
                "over http:// instead — the credentials and port are unchanged"
            )
        if parsed.username and parsed.password:
            launch["proxy"] = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                "username": parsed.username,
                "password": parsed.password,
            }
        else:
            launch["proxy"] = {"server": settings.proxy}
    return launch


# Residential gateways hand out a different exit IP per connection unless the
# username pins one. That breaks this design at both seams: the challenge and the
# reload that validates its token leave from different addresses, and every later
# httpx poll leaves from another one again, so a token that was minted fine is
# refused on first use. Providers spell the parameter differently, so this only
# looks for the shape of one.
STICKY_MARKERS = ("sessid", "session", "sess-", "sess.")
STICKY_HINT = (
    "the proxy username pins no session, so the exit IP can change between the "
    "challenge and the reload that validates its token — and again on every "
    "poll after that. Add your provider's sticky-session parameter "
    "(DataImpulse: 'user__cr.in;sessid.instamart;sessttl.60')"
)


def _warn_if_rotating(settings: Settings) -> None:
    if not settings.proxy:
        return
    user = (urllib.parse.urlparse(settings.proxy).username or "").lower()
    if not any(m in user for m in STICKY_MARKERS):
        log.warning(STICKY_HINT)


# A headless poller renders nothing, so every byte spent on things that exist
# only to be looked at is pure proxy spend — and on residential bandwidth that is
# metered by the gigabyte. Product thumbnails dominate an Instamart page. Scripts
# and stylesheets are deliberately *not* blocked: the challenge itself is a
# script, and a browser that fetches no CSS at all is one worth fingerprinting.
COSMETIC_RESOURCES = frozenset({"image", "media", "font"})


def _block_cosmetics(ctx: Any) -> None:
    """Drop image/media/font requests before they leave the machine."""

    def route(r: Any) -> None:
        try:
            if r.request.resource_type in COSMETIC_RESOURCES:
                r.abort()
            else:
                r.continue_()
        except Exception:  # noqa: BLE001 — a dead route must not kill the page
            pass

    ctx.route("**/*", route)


def _context_options() -> dict[str, Any]:
    return {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
    }


INSTAMART_URL = "https://www.swiggy.com/instamart"

PROFILE_DIR = "chromium-profile"


def _open_context(pw: Any, settings: Settings) -> tuple[Any, Any]:
    """A Chromium context and its page, reusing one profile where it can.

    The WAF serves `challenge.js` — ~300 KB — on every challenge round, and a
    browser launched fresh has an empty cache, so it is paid for on every single
    bootstrap. A profile that survives between runs turns that into a conditional
    request. Chromium will not share a profile across processes, so a second
    caller (the CLI, say, while the panel is polling) quietly gets a throwaway
    one instead of failing: a cold cache is a cost, not an error.
    """
    if settings.browser_profile:
        profile = settings.data_dir / PROFILE_DIR
        try:
            profile.mkdir(parents=True, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                str(profile), **_launch_options(settings), **_context_options()
            )
            # A persistent context opens with a blank tab already in hand.
            return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001 — any profile problem is non-fatal
            log.warning(
                "could not open the cached browser profile (%s); "
                "falling back to a cold one",
                e,
            )

    browser = pw.chromium.launch(**_launch_options(settings))
    ctx = browser.new_context(**_context_options())
    return ctx, ctx.new_page()


def _close_context(ctx: Any) -> None:
    """Close a context and, if it has one, the browser behind it.

    A persistent context owns its browser and reports `.browser` as None; a
    normal one does not, and leaks a Chromium if only the context is closed.
    """
    browser = None
    try:
        browser = ctx.browser
    except Exception:  # noqa: BLE001 — teardown must never raise
        pass
    for obj in (ctx, browser):
        try:
            if obj is not None:
                obj.close()
        except Exception:  # noqa: BLE001
            pass

# How long the challenge script gets to make progress on its own before we
# reload for it, and how many times we are willing to do that. Reloading is not
# free: it cancels whatever round is running, and the WAF counts cancelled rounds
# against an attempt limit, so an impatient loop does not merely fail — it burns
# the budget that would have let a patient one through.
SELF_RELOAD_GRACE = 6.0
MAX_RELOADS = 3
POLL_MS = 500


def _jar(ctx: Any) -> dict[str, str]:
    return {c["name"]: c["value"] for c in ctx.cookies()}


def _cleared(cookies: dict[str, str]) -> bool:
    """A cleared session carries the token *and* the cookies the site itself sets."""
    return "aws-waf-token" in cookies and len(cookies) > 1


def _exhausted(page: Any) -> bool:
    """True once the WAF has stopped answering this client's challenges."""
    try:
        return any(m in page.content().lower() for m in EXHAUSTED_MARKERS)
    except Exception:  # noqa: BLE001 — a page mid-navigation is not an answer
        return False


def _clear_challenge(
    page: Any, ctx: Any, seconds: float, *, status: int | None = None
) -> tuple[dict[str, str], int | None]:
    """Wait for the challenge to *clear*, not merely for a token to appear.

    The WAF answers the first request with a 202 interstitial whose script solves
    a proof of work, sets `aws-waf-token`, and then reloads the page itself. Only
    that reload validates the token: one lifted straight out of the cookie jar is
    answered 202 on its very first use, which looks exactly like an IP problem
    and is not one.

    The trap is reloading *for* the script. A `goto` mid-challenge cancels the
    round, and the WAF only allows a few cancelled rounds before it stops issuing
    challenges altogether — a hard block, arrived at from a position where doing
    nothing would have worked. So the script gets first refusal: this watches the
    navigations it makes on its own and only steps in once nothing has happened
    for `SELF_RELOAD_GRACE`, at most `MAX_RELOADS` times.
    """
    # The challenge script's own reload is a navigation we did not issue, so its
    # status has to be caught in flight rather than read off a `goto` result.
    nav: dict[str, Any] = {"status": status, "at": time.monotonic()}

    def on_response(r: Any) -> None:
        try:
            if r.request.resource_type == "document" and r.url.startswith(
                "https://www.swiggy.com/"
            ):
                nav["status"], nav["at"] = r.status, time.monotonic()
        except Exception:  # noqa: BLE001 — a listener must never raise
            pass

    page.on("response", on_response)
    try:
        deadline = time.monotonic() + seconds
        reloads = 0
        token_at: float | None = None
        last_token: str | None = None

        while True:
            cookies = _jar(ctx)
            if nav["status"] == 200 and _cleared(cookies):
                return cookies, nav["status"]

            token = cookies.get("aws-waf-token")
            if token and token != last_token:
                # A fresh challenge round just landed; it gets its own grace.
                last_token, token_at = token, time.monotonic()

            # Only intervene in a challenge that has stalled: a token exists, so
            # the script got that far, but neither it nor the page has moved
            # since. Without a token there is nothing a reload could validate,
            # and restarting the round from scratch only spends another attempt.
            idle = time.monotonic() - max(token_at or 0.0, nav["at"])
            if token_at is not None and idle >= SELF_RELOAD_GRACE and reloads < MAX_RELOADS:
                if _exhausted(page):
                    log.debug("the WAF has stopped serving challenges; not reloading")
                    return cookies, nav["status"]
                reloads += 1
                response = page.goto(
                    INSTAMART_URL, wait_until="domcontentloaded", timeout=60_000
                )
                if response is not None:
                    nav["status"], nav["at"] = response.status, time.monotonic()
                cookies = _jar(ctx)
                if nav["status"] == 200 and _cleared(cookies):
                    return cookies, nav["status"]
                log.debug(
                    "token not accepted (HTTP %s, %d cookies); reload %d/%d",
                    nav["status"],
                    len(cookies),
                    reloads,
                    MAX_RELOADS,
                )
                # That reload served a new challenge. Let it run before judging.
                last_token, token_at = _jar(ctx).get("aws-waf-token"), time.monotonic()

            if time.monotonic() >= deadline:
                return _jar(ctx), nav["status"]
            page.wait_for_timeout(POLL_MS)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass


def mint_token(settings: Settings) -> SessionData:
    """Launch headless Chromium, clear the WAF challenge, keep the cookies."""
    from playwright.sync_api import sync_playwright  # imported lazily: slow

    log.info(
        "minting a fresh WAF token via headless Chromium (up to %ds)",
        settings.bootstrap_seconds,
    )
    if settings.proxy:
        log.info("bootstrap is going out through the proxy")
        _warn_if_rotating(settings)

    status: int | None = None
    html = ""
    failure_shot: bytes | None = None

    with sync_playwright() as p:
        ctx, page = _open_context(p, settings)
        if settings.block_images:
            _block_cosmetics(ctx)
        # Minting means *replacing* the token, and a reused profile still holds
        # the last one. Drop the cookies, keep the cache — that is the whole
        # point of the profile.
        ctx.clear_cookies()
        opened = page.goto(INSTAMART_URL, wait_until="domcontentloaded", timeout=60_000)

        cookies, status = _clear_challenge(
            page,
            ctx,
            settings.bootstrap_seconds,
            status=opened.status if opened else None,
        )
        if not _cleared(cookies):
            # Grab the evidence before the browser goes away.
            try:
                html = page.content()
                failure_shot = page.screenshot(full_page=False)
            except Exception:  # noqa: BLE001 — diagnostics must not mask the error
                pass
        _close_context(ctx)

    if not _cleared(cookies):
        why = _diagnose(html, cookies)
        _save_failure(settings, html, failure_shot)
        log.error(
            "bootstrap failed: HTTP %s, %d cookies, %d bytes of HTML — %s",
            status,
            len(cookies),
            len(html),
            why,
        )
        raise Blocked(f"challenge not cleared: {why}")

    log.info("bootstrap succeeded (HTTP %s, %d cookies)", status, len(cookies))
    # deviceId arrives signed as `s:<uuid>.<signature>`; the API wants the uuid.
    raw = urllib.parse.unquote(cookies.get("deviceId", ""))
    device_id = raw.removeprefix("s:").split(".")[0]
    return SessionData(cookies=cookies, device_id=device_id)


def _save_failure(settings: Settings, html: str, shot: bytes | None) -> None:
    """Keep the page that refused us, so the panel can show it."""
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if shot:
            (settings.data_dir / "bootstrap-failure.png").write_bytes(shot)
        if html:
            (settings.data_dir / "bootstrap-failure.html").write_text(
                html[:400_000], encoding="utf-8"
            )
    except OSError as e:
        log.warning("could not save bootstrap diagnostics: %s", e)

# ── browser transport ────────────────────────────────────────────────
#
# The cheap path hands the browser's `aws-waf-token` to httpx. That works right
# up until the WAF stops trusting the caller, because the cookie is only half of
# what it checks — the TLS handshake and header order are the other half, and
# httpx's do not look like Chromium's. On a residential IP the mismatch is
# tolerated; on a datacenter IP it routinely is not, which shows up as a freshly
# minted token being answered `202` on its very first use.
#
# This transport removes the mismatch by never leaving the browser: calls go out
# as `fetch()` from the page that solved the challenge, so the fingerprint, the
# cookies and the JS environment are all the ones the token was issued to. It is
# far slower — a Chromium per pass rather than a one-second HTTP call — so it is
# the fallback, not the default.

# The page can navigate under us (SPA routing, or the challenge reloading it),
# which destroys the execution context the fetch was running in.
EVAL_ATTEMPTS = 4

FETCH_JS = """
async ({ url, method, body, headers }) => {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body === null ? undefined : body,
      credentials: 'include',
    });
  } catch (e) {
    return { error: String(e) };
  }
  const text = await res.text();
  const out = {};
  res.headers.forEach((v, k) => { out[k] = v; });
  return { status: res.status, headers: out, text };
}
"""


@dataclass
class BrowserResponse:
    """Enough of an httpx.Response for `request()` and the callers above it."""

    status_code: int
    headers: dict[str, str]
    text: str

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> BrowserResponse:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )
        return self


class BrowserCookies:
    """`client.cookies` for the browser transport.

    Reads mirror the live browser jar and writes push into it, so the existing
    `client.cookies.set(...)` calls in instamart.py and `sync_cookies` both keep
    working against a page instead of an httpx client.
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._jar = httpx.Cookies()
        self.refresh()

    @property
    def jar(self):  # noqa: ANN201 — matches httpx.Cookies.jar
        return self._jar.jar

    def refresh(self) -> None:
        fresh = httpx.Cookies()
        for c in self._ctx.cookies():
            fresh.set(c["name"], c["value"], domain=COOKIE_DOMAIN)
        self._jar = fresh

    def set(self, name: str, value: str, domain: str = COOKIE_DOMAIN, **_: Any) -> None:
        self._jar.set(name, value, domain=domain)
        self._ctx.add_cookies(
            [{"name": name, "value": value, "domain": domain, "path": "/"}]
        )

    def get(self, name: str, default: Any = None) -> Any:
        return self._jar.get(name, default)


class BrowserClient:
    """An httpx-shaped client that issues its calls from inside Chromium.

    Playwright's sync API is bound to the thread that started it, so this must
    be opened, used and closed on one thread. The scheduler already serialises
    every Instamart call, which is what makes that safe here.
    """

    def __init__(self, settings: Settings, data: SessionData) -> None:
        self._settings = settings
        self._data = data
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self.cookies: BrowserCookies | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    def open(self) -> SessionData:
        """Park a browser on swiggy.com holding a live token."""
        from playwright.sync_api import sync_playwright

        log.info("opening a browser session (calls will go out from the page)")
        _warn_if_rotating(self._settings)
        self._pw = sync_playwright().start()
        try:
            self._ctx, self._page = _open_context(self._pw, self._settings)
            if self._settings.block_images:
                _block_cosmetics(self._ctx)
            # A reused profile carries the last run's cookies; the seeding below
            # is the intended jar, so start from a known-empty one.
            self._ctx.clear_cookies()
            # Seed whatever we already have; a still-good token means the page
            # loads without a challenge and we save the wait.
            if self._data.cookies:
                self._ctx.add_cookies(
                    [
                        {
                            "name": n,
                            "value": v,
                            "domain": COOKIE_DOMAIN,
                            "path": "/",
                        }
                        for n, v in self._data.cookies.items()
                    ]
                )
            opened = self._page.goto(
                INSTAMART_URL, wait_until="domcontentloaded", timeout=60_000
            )

            cookies, status = _clear_challenge(
                self._page,
                self._ctx,
                self._settings.bootstrap_seconds,
                status=opened.status if opened else None,
            )
            # Instamart is a SPA and redirects itself once the challenge
            # clears. Fetching mid-navigation destroys the execution context,
            # so let it come to rest before anyone calls request().
            self._settle()

            if not _cleared(cookies):
                html, shot = "", None
                try:
                    html, shot = self._page.content(), self._page.screenshot()
                except Exception:  # noqa: BLE001 — diagnostics only
                    pass
                why = _diagnose(html, cookies)
                _save_failure(self._settings, html, shot)
                log.error(
                    "browser session not cleared: HTTP %s, %d cookies — %s",
                    status,
                    len(cookies),
                    why,
                )
                raise Blocked(f"challenge not cleared: {why}")
        except BaseException:
            self.close()
            raise

        self._data.cookies = cookies
        if not self._data.device_id:
            raw = urllib.parse.unquote(cookies.get("deviceId", ""))
            self._data.device_id = raw.removeprefix("s:").split(".")[0]
        self.cookies = BrowserCookies(self._ctx)
        log.info("browser session ready (%d cookies)", len(cookies))
        return self._data

    def _settle(self) -> None:
        """Wait for the SPA to stop navigating. Best effort — it may never idle."""
        for state in ("load", "networkidle"):
            try:
                self._page.wait_for_load_state(state, timeout=10_000)
            except Exception:  # noqa: BLE001 — a busy SPA is not a failure
                pass

    def close(self) -> None:
        if self._ctx is not None:
            _close_context(self._ctx)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass
        self._browser = self._pw = self._ctx = self._page = None

    # ── the httpx.Client surface `request()` uses ────────────────────
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        **_: Any,
    ) -> BrowserResponse:
        if self._page is None:
            raise RuntimeError("browser client is not open")

        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {
            "accept": "*/*",
            "x-build-version": self._settings.build_version,
            "x-device-id": self._data.device_id,
        }
        body = None
        if json is not None:
            headers["content-type"] = "application/json"
            body = _json.dumps(json)

        payload = {"url": url, "method": method, "body": body, "headers": headers}
        result = self._evaluate(payload)
        if "error" in result:
            # Same shape of failure as a dead tunnel, so the retry ladder above
            # treats it the same way.
            raise httpx.ConnectError(f"in-page fetch failed: {result['error']}")

        self.cookies.refresh()
        return BrowserResponse(
            status_code=result["status"],
            headers=result["headers"],
            text=result["text"],
        )

    def _evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the fetch, riding out any navigation that lands mid-call."""
        last: Exception | None = None
        for attempt in range(EVAL_ATTEMPTS):
            try:
                return self._page.evaluate(FETCH_JS, payload)
            except Exception as e:  # noqa: BLE001 — Playwright's own Error type
                if "context was destroyed" not in str(e) and "navigating" not in str(e):
                    raise
                last = e
                log.debug("page navigated mid-fetch, settling (%d)", attempt + 1)
                self._settle()
        raise httpx.ConnectError(f"page kept navigating away: {last}")


def build_client(settings: Settings, data: SessionData) -> httpx.Client:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-IN",
        "content-type": "application/json",
        "x-build-version": settings.build_version,
        "x-device-id": data.device_id,
        "Origin": "https://www.swiggy.com",
        "Referer": "https://www.swiggy.com/instamart",
    }
    # Pin every cookie to the Swiggy domain. Seeding httpx from a bare dict
    # files them under an empty domain, so a `Set-Cookie` for `.swiggy.com`
    # would land *beside* the old value rather than replacing it, leaving
    # `sync_cookies` two `aws-waf-token`s to pick between.
    jar = httpx.Cookies()
    for name, value in data.cookies.items():
        jar.set(name, value, domain=COOKIE_DOMAIN)

    return httpx.Client(
        headers=headers,
        cookies=jar,
        timeout=30.0,
        follow_redirects=True,
        proxy=settings.proxy,
    )


def sync_cookies(client: httpx.Client, data: SessionData) -> bool:
    """Fold the server's rotated cookies back onto `data`. True if any changed.

    The WAF issues a replacement `aws-waf-token` as the session is used. Without
    this the cache keeps replaying whatever the browser minted, which the server
    has long retired by the next poll — so every pass would open on a token that
    is already dead and pay for a fresh browser bootstrap.
    """
    merged = data.cookies | {c.name: c.value for c in client.cookies.jar}
    if merged == data.cookies:
        return False
    data.cookies = merged
    return True


def request(client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    """Issue a call, converting WAF refusals into `Blocked`."""
    r = client.request(method, url, **kw)
    if r.status_code in BLOCKED:
        raise Blocked(f"{method} {url.removeprefix(API)} -> HTTP {r.status_code}")
    r.raise_for_status()

    # A challenge also turns up as a 200 wrapping an HTML interstitial, or as an
    # empty body. Every endpoint here answers with JSON, so anything else is a
    # block wearing a different hat. Catching it now means the caller fails loudly
    # instead of much later on `.json()` — or, for select-location, on a regex
    # that quietly finds no storeId in a page that never held one.
    content_type = r.headers.get("content-type", "")
    if "json" not in content_type.lower() or not r.content.strip():
        raise Blocked(
            f"{method} {url.removeprefix(API)} -> HTTP {r.status_code} "
            f"with a {content_type or 'missing'} body, not JSON"
        )
    return r
