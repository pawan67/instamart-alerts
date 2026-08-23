"""One polling pass: refresh the session if needed, run every watch, alert on hits."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .instamart import Product, ensure_location, search
from .notify import format_alert, send
from .session import Blocked, SessionData, build_client, load_cached, mint_token, save_cached
from .state import AlertState
from .watchlist import Watch, Watchlist

log = logging.getLogger(__name__)


@dataclass
class WatchResult:
    watch: Watch
    candidates: list[Product]  # products matching the watch's filters
    hits: list[Product]  # candidates at or above the discount threshold
    alerted: list[Product]  # hits that were actually sent (post de-dup)
    error: str | None = None


def open_session(settings: Settings, *, force_refresh: bool = False):
    """Return (client, session_data). Re-mints the WAF token when required."""
    data = None if force_refresh else load_cached(settings)
    if data is None or not data.cookies:
        data = mint_token(settings)
        save_cached(settings, data)
    return build_client(settings, data), data


def run_once(
    settings: Settings,
    watchlist: Watchlist,
    *,
    dry_run: bool = False,
    cooldown_hours: float = 24.0,
) -> list[WatchResult]:
    client, data = open_session(settings)
    try:
        results = _run(settings, watchlist, client, data, dry_run, cooldown_hours)
    except Blocked as e:
        # Cached token went stale — mint a new one and retry once.
        log.warning("session blocked (%s); re-minting", e)
        client.close()
        client, data = open_session(settings, force_refresh=True)
        results = _run(settings, watchlist, client, data, dry_run, cooldown_hours)
    finally:
        client.close()
    return results


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
