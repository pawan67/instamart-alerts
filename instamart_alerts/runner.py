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
    save_cached,
    sync_cookies,
)
from .state import AlertState
from .watchlist import Watch, Watchlist

log = logging.getLogger(__name__)

# A blocked pass usually just means a stale token, but the replacement is only
# accepted about half the time — the WAF sometimes hands out a token it has
# already decided to re-challenge. Each retry costs a ~30s browser bootstrap,
# so the ladder stays short.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (5.0, 20.0)

# Anything that fails before Swiggy sees the request: a proxy that accepts the
# connection then drops the TLS handshake, a timeout, a dead tunnel. Retryable
# on the same session, since the token was never the problem.
TRANSPORT_ERRORS = httpx.TransportError


@dataclass
class WatchResult:
    watch: Watch
    candidates: list[Product]  # products matching the watch's filters
    hits: list[Product]  # candidates at or above the discount threshold
    alerted: list[Product]  # hits that were actually sent (post de-dup)
    error: str | None = None


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
        data = mint_token(settings)
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
            except (Blocked, TRANSPORT_ERRORS) as e:
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
                    delay = BACKOFF_SECONDS[attempt - 1]
                    log.warning(
                        "%s (%s); %s in %.0fs [attempt %d/%d]",
                        what,
                        e,
                        action,
                        delay,
                        attempt + 1,
                        MAX_ATTEMPTS,
                    )
                    time.sleep(delay)
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

    for watch in watchlist.active:
        try:
            products = search(client, data.store_id, watch.query)
        except Blocked:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.error("watch %r failed: %s", watch.name, e)
            results.append(WatchResult(watch, [], [], [], error=str(e)))
            continue

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

    if not dry_run:
        state.prune(live_keys)
        state.save()
    return results
