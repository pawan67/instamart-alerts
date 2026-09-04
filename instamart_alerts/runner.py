"""One polling pass: refresh the session if needed, run every watch, alert on hits."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .instamart import Product, ensure_location, search
from .notify import format_alert, send
from .session import (
    Blocked,
    BrowserClient,
    SessionData,
    build_client,
    load_cached,
    mint_token,
    new_session_id,
    save_cached,
    sync_cookies,
)
from .state import AlertState
from .watchlist import Watch, Watchlist

log = logging.getLogger(__name__)

# A blocked pass usually just means a stale token, but the replacement is only
# accepted about half the time — the WAF issues tokens from exits it has already
# decided against, and that verdict belongs to the address, not to us. Waiting
# does not change it; asking for another exit does, and every rung here mints
# behind a new sticky session. So the ladder runs longer than a cool-off backoff
# would want and its sleeps stay short: what buys a pass is another draw, not
# another minute. Each rung still costs a ~30s bootstrap, which is what keeps
# this a ladder and not a loop.
BACKOFF_SECONDS = (5.0, 15.0, 20.0, 30.0)
# One rung per sleep, plus the first attempt, which does not sleep at all.
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

# Anything that fails before Swiggy sees the request: a proxy that accepts the
# connection then drops the TLS handshake, a timeout, a dead tunnel. Retryable
# on the same session, since the token was never the problem.
#
# socksio raises through httpx's mapping rather than into it — a SOCKS proxy
# that answers the handshake with garbage surfaces as socksio.ProtocolError,
# which is not an httpx exception and so used to escape this ladder entirely and
# kill the whole pass. It is the most ordinary proxy failure there is.
_PROXY_ERRORS: tuple[type[BaseException], ...] = ()
try:
    from socksio.exceptions import ProtocolError as _SocksProtocolError

    _PROXY_ERRORS = (_SocksProtocolError,)
except ImportError:  # httpx installed without the socks extra
    pass

TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    *_PROXY_ERRORS,
)


@dataclass
class WatchResult:
    watch: Watch
    candidates: list[Product]  # products matching the watch's filters
    hits: list[Product]  # candidates at or above the discount threshold
    alerted: list[Product]  # hits that were actually sent (post de-dup)
    error: str | None = None


class EmptyPass(RuntimeError):
    """Every watch ran, and between them they found nothing at all.

    Not a refusal in the WAF's own terms — the calls came back 200 with valid
    JSON — but a token it has soured on stops refusing and starts agreeing that
    the store is empty, which is indistinguishable from a real answer until the
    same question is put to a different token. Carries the results so the last
    attempt can hand them back rather than throw them away.
    """

    def __init__(self, results: list[WatchResult]) -> None:
        super().__init__(f"{len(results)} watches, 0 results between them")
        self.results = results


def open_session(
    settings: Settings,
    *,
    force_refresh: bool = False,
    previous: SessionData | None = None,
    browser: bool = False,
):
    """Return (client, session_data). Re-mints the WAF token when required.

    With `browser=True` the client issues its calls from inside Chromium instead
    of over httpx — slower, but the only thing that works when the WAF is
    checking more than the cookie.
    """
    data = None if force_refresh else load_cached(settings)

    if browser:
        # The browser client mints its own token as a side effect of opening,
        # so there is nothing to bootstrap separately.
        prior = data if data is not None else (previous or load_cached(settings))
        session = SessionData() if data is None else data
        if prior is not None and prior.store_id:
            session.store_id = prior.store_id
            session.area_label = prior.area_label
            session.lat, session.lng = prior.lat, prior.lng
        client = BrowserClient(settings, session)
        data = client.open()
        save_cached(settings, data)
        return client, data

    if data is None or not data.cookies:
        prior = previous or load_cached(settings)
        # Never behind the exit that was just refused: whatever the last session
        # used, this one asks for another.
        data = mint_token(settings, new_session_id())
        # A new token does not invalidate the store lookup. Carrying it over
        # keeps the fresh session from spending its first two calls — the ones
        # most likely to be re-challenged — re-geocoding an area we resolved
        # minutes ago.
        if prior is not None and prior.store_id:
            data.store_id = prior.store_id
            data.area_label = prior.area_label
            data.lat, data.lng = prior.lat, prior.lng
        save_cached(settings, data)
    return build_client(settings, data), data


def use_browser_on(attempt: int, settings: Settings) -> bool:
    """Which transport attempt `attempt` should use."""
    if settings.transport == "browser":
        return True
    if settings.transport == "http":
        return False
    # auto: spend the cheap attempts on httpx, then stop guessing.
    return attempt >= MAX_ATTEMPTS


def _pause(attempt: int, what: str, action: str, why: BaseException) -> None:
    """Announce the retry and sleep this rung of the ladder."""
    delay = BACKOFF_SECONDS[attempt - 1]
    log.warning(
        "%s (%s); %s in %.0fs [attempt %d/%d]",
        what,
        why,
        action,
        delay,
        attempt + 1,
        MAX_ATTEMPTS,
    )
    time.sleep(delay)


def run_once(
    settings: Settings,
    watchlist: Watchlist,
    *,
    dry_run: bool = False,
    cooldown_hours: float = 24.0,
) -> list[WatchResult]:
    client: httpx.Client | None = None
    data: SessionData | None = None
    failure: Exception | None = None

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if client is None:
                    browser = use_browser_on(attempt, settings)
                    if browser and attempt > 1:
                        log.info("retrying through the browser transport")
                    client, data = open_session(
                        settings,
                        force_refresh=attempt > 1,
                        previous=data,
                        browser=browser,
                    )
                results = _run(
                    settings, watchlist, client, data, dry_run, cooldown_hours
                )
            except EmptyPass as e:
                failure = e
                if attempt == MAX_ATTEMPTS:
                    # Out of rungs, so believe it: a watchlist really can have
                    # nothing in it, and several independent tokens agreeing is
                    # the strongest evidence available from here.
                    log.warning("%s on every attempt — taking it at face value", e)
                    results = e.results
                else:
                    # Nothing was recorded and nothing was sent, so there is no
                    # half-finished pass to unwind — drop the token that
                    # answered this way and put it to a different exit.
                    if client is not None:
                        client.close()
                    client = None
                    _pause(attempt, "empty pass", "re-minting", e)
                    continue
            except (Blocked, *TRANSPORT_ERRORS) as e:
                failure = e
                if isinstance(e, Blocked):
                    # A refused token only improves by being replaced, so drop
                    # the client and let the next attempt mint a new one.
                    if client is not None:
                        client.close()
                    client = None
                    what, action = "session blocked", "re-minting"
                else:
                    # The tunnel died before Swiggy saw us; the token is still
                    # good. Keep the session and just redial.
                    what, action = "connection failed", "reconnecting"
                if attempt < MAX_ATTEMPTS:
                    _pause(attempt, what, action, e)
                continue

            # Swiggy rotates aws-waf-token as the session is used; keep the
            # newest one so the next poll does not open on a retired token.
            if sync_cookies(client, data):
                save_cached(settings, data)
            return results
    finally:
        if client is not None:
            client.close()

    log.error("giving up after %d attempts (%s)", MAX_ATTEMPTS, failure)
    raise failure


def _run(
    settings: Settings,
    watchlist: Watchlist,
    client: httpx.Client,
    data: SessionData,
    dry_run: bool,
    cooldown_hours: float,
) -> list[WatchResult]:
    if ensure_location(client, data, settings.area):
        save_cached(settings, data)

    state = AlertState.load(settings.data_dir / "alerts.json", cooldown_hours)
    results: list[WatchResult] = []
    live_keys: set[str] = set()
    found = 0  # products returned across every search, before any filtering

    for watch in watchlist.active:
        try:
            products = search(client, data.store_id, watch.query)
        except (Blocked, *TRANSPORT_ERRORS):
            # Neither is this watch's fault. A dead tunnel used to be recorded
            # as "this watch failed" and the pass carried on, so a flaky proxy
            # silently dropped a watch for that round instead of redialling.
            raise
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.error("watch %r failed: %s", watch.name, e)
            results.append(WatchResult(watch, [], [], [], error=str(e)))
            continue

        found += len(products)
        candidates = [p for p in products if watch.matches(p)]
        hits = [p for p in candidates if watch.is_hit(p)]
        log.info(
            "%s: %d results, %d after filters, %d over %.0f%%",
            watch.name,
            len(products),
            len(candidates),
            len(hits),
            watch.min_discount_pct,
        )

        to_send: list[Product] = []
        for p in candidates:
            live_keys.add(f"{watch.name}::{p.sku_id}")
            hit = watch.is_hit(p)
            if hit and state.should_alert(watch.name, p):
                to_send.append(p)
            # Record every candidate so a lapsed-then-returned deal re-fires.
            if not dry_run:
                state.record(watch.name, p, hit=hit)

        sent: list[Product] = []
        if to_send and not dry_run:
            if send(settings, format_alert(watch.name, data.area_label, to_send)):
                sent = to_send
            else:
                # Delivery failed — forget it so the next pass retries.
                for p in to_send:
                    state.seen.pop(f"{watch.name}::{p.sku_id}", None)
        elif to_send:
            sent = to_send  # dry run: report what would have gone out

        results.append(WatchResult(watch, candidates, hits, sent))

    # Every watch coming back empty at once is not what a live store looks like.
    # It used to be reported as a finished pass — "0 tracked, best 0%" — so a
    # deal running during one of those windows went unseen and unlogged. Raise
    # before anything is persisted and let the ladder ask a fresh token instead.
    # A watch that errored is its own, already-reported problem and leaves the
    # pass with no opinion to offer, so this asks for a clean sweep: every watch
    # answered, and every one of them answered with nothing.
    if results and not found and all(r.error is None for r in results):
        raise EmptyPass(results)

    if not dry_run:
        state.prune(live_keys)
        state.save()
    return results
