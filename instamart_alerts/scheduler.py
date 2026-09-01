"""The background poller behind the control panel's ON/OFF switch.

One worker thread owns every Instamart call the panel can trigger, scheduled or
manual, because the WAF does not reward parallelism and a browser bootstrap must
never run twice at once. Manual jobs jump the queue by taking the same lock; the
poller checks for a stop signal between passes rather than mid-flight, so a
running check always finishes and records its result.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, logbus
from .instamart import Product
from .runner import WatchResult, run_once
from .watchlist import Watchlist

log = logging.getLogger("scheduler")

# Poll sleeps in slices so toggling the switch off is felt immediately.
TICK_SECONDS = 1.0

# A pass that dies on the WAF carries no information the next pass can use: the
# only thing that changes a verdict on an exit IP is time. Polling straight
# through a block also feeds it, because every attempt starts challenge rounds
# the WAF counts against us — so a run of failures does not just waste a browser
# bootstrap, it deepens the hole. Each consecutive failure widens the gap, up to
# a ceiling; one success puts it straight back to the configured interval.
FAILURE_BACKOFF = (2, 4, 8)
MAX_BACKOFF_MINUTES = 120


# How many tracked products travel with a run event. The whole candidate list is
# what makes a run reviewable — "best 36.1%" tells you nothing about the shape of
# the spread below it — but the events are kept in memory and replayed to every
# tab, so the tail of a large search is summarised rather than carried.
MAX_TRACKED_REPORTED = 40


def _item(p: Product, threshold: float) -> dict[str, Any]:
    return {
        "name": p.name,
        "quantity": p.quantity,
        "price": p.price,
        "mrp": p.mrp,
        "discount_pct": p.discount_pct,
        "url": p.url,
        "in_stock": p.in_stock,
        "hit": p.discount_pct >= threshold,
    }


def summarise(results: list[WatchResult]) -> list[dict[str, Any]]:
    out = []
    for r in results:
        threshold = r.watch.min_discount_pct
        ranked = sorted(r.candidates, key=lambda x: -x.discount_pct)
        alerted_skus = {p.sku_id for p in r.alerted}
        out.append(
            {
                "name": r.watch.name,
                "query": r.watch.query,
                "threshold": threshold,
                "tracked": len(r.candidates),
                "best": round(max((p.discount_pct for p in ranked), default=0.0), 1),
                "hits": len(r.hits),
                "error": r.error,
                "alerted": [
                    _item(p, threshold)
                    for p in sorted(r.alerted, key=lambda x: -x.discount_pct)
                ],
                # Everything that passed the filters, best discount first, so a
                # run can be read as "how close did we get" and not just
                # "did anything fire".
                "tracked_items": [
                    _item(p, threshold) | {"alerted": p.sku_id in alerted_skus}
                    for p in ranked[:MAX_TRACKED_REPORTED]
                ],
                "tracked_omitted": max(0, len(ranked) - MAX_TRACKED_REPORTED),
            }
        )
    return out


@dataclass
class Scheduler:
    """Owns the poll loop and the lock every Instamart call goes through."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _wake: threading.Event = field(default_factory=threading.Event)
    _busy: str = ""
    # Claimed synchronously by run_in_background so two clicks a millisecond
    # apart cannot both get past the busy check and double-send alerts.
    _claim: threading.Lock = field(default_factory=threading.Lock)
    _claimed: bool = False
    _next_run: float = 0.0
    _last_run: float = 0.0
    _last_error: str = ""
    _minutes: int = config.DEFAULT_POLL_MINUTES
    # Consecutive failed scheduled passes, which is what widens the interval.
    _failures: int = 0

    # ── introspection ────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "busy": self._busy,
            "minutes": self._minutes,
            "next_run": self._next_run if self.running else 0.0,
            "last_run": self._last_run,
            "last_error": self._last_error,
        }

    def _broadcast(self) -> None:
        logbus.publish({"type": "status", "scheduler": self.status()})

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self, minutes: int) -> None:
        self._minutes = max(1, int(minutes))
        if self.running:
            # Already up — just re-target the interval and reschedule. Asking
            # for an interval is asking for it now, not after a backoff.
            self._failures = 0
            self._next_run = time.time() + self._minutes * 60
            self._wake.set()
            log.info("poller interval set to %d min", self._minutes)
            self._broadcast()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self._thread.start()
        log.info("poller started — every %d min", self._minutes)
        self._broadcast()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._wake.set()
        log.info("poller stopped")
        self._next_run = 0.0
        self._broadcast()

    def run_now(self) -> None:
        """Ask the loop to poll immediately instead of waiting out the interval."""
        if self.running:
            self._next_run = 0.0
            self._wake.set()

    def _loop(self) -> None:
        # First pass fires straight away so switching the poller on gives
        # feedback now rather than in fifteen minutes.
        self._next_run = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now >= self._next_run:
                self._poll()
                self._next_run = time.time() + self._gap_minutes() * 60
                self._broadcast()
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()
        self._thread = None
        self._broadcast()

    def _gap_minutes(self) -> int:
        """Minutes until the next scheduled pass, widened by any failing run."""
        if not self._failures:
            return self._minutes
        factor = FAILURE_BACKOFF[min(self._failures, len(FAILURE_BACKOFF)) - 1]
        minutes = min(self._minutes * factor, MAX_BACKOFF_MINUTES)
        log.warning(
            "%d checks in a row failed — backing off to %d min to let the "
            "block age out instead of feeding it",
            self._failures,
            minutes,
        )
        return minutes

    def _poll(self) -> None:
        settings = config.load()
        if config.in_quiet_hours(settings):
            log.info(
                "quiet hours — no scheduled checks until %02d:00 IST",
                settings.quiet_end,
            )
            return
        if not settings.area:
            log.warning("skipping poll — no delivery area set")
            return
        watchlist = Watchlist.load(settings.watchlist_path)
        if not watchlist.active:
            log.warning("skipping poll — no enabled watches")
            return
        self.execute(
            "scheduled check",
            lambda: run_once(
                settings, watchlist, cooldown_hours=settings.cooldown_hours
            ),
            trigger="scheduled",
        )

    # ── the one place Instamart work happens ─────────────────────────
    def execute(
        self,
        label: str,
        job: Callable[[], list[WatchResult]],
        *,
        trigger: str = "manual",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run a watchlist pass under the lock and publish the outcome."""
        with self.lock:
            self._busy = label
            self._broadcast()
            started = time.time()
            log.info("%s: starting", label)
            try:
                results = job()
            except Exception as e:  # noqa: BLE001 — reported, never fatal
                log.exception("%s failed: %s", label, e)
                self._last_error = str(e)
                if trigger == "scheduled":
                    self._failures += 1
                event = {
                    "type": "run",
                    "trigger": trigger,
                    "dry_run": dry_run,
                    "ok": False,
                    "error": str(e),
                    "duration": round(time.time() - started, 1),
                    "results": [],
                }
                logbus.publish(event)
                return event
            finally:
                self._busy = ""
                self._claimed = False
                self._last_run = time.time()
                self._broadcast()

        self._last_error = ""
        # A manual run clears it too: it is the same evidence that the WAF has
        # let go, and there is no reason to sit out a backoff after seeing it.
        self._failures = 0
        summary = summarise(results)
        alerted = sum(len(r["alerted"]) for r in summary)
        log.info(
            "%s: done in %.1fs — %d watch%s, %d alert%s %s",
            label,
            time.time() - started,
            len(summary),
            "" if len(summary) == 1 else "es",
            alerted,
            "" if alerted == 1 else "s",
            "(dry run, nothing sent)" if dry_run else "sent",
        )
        event = {
            "type": "run",
            "trigger": trigger,
            "dry_run": dry_run,
            "ok": True,
            "error": "",
            "duration": round(time.time() - started, 1),
            "results": summary,
        }
        logbus.publish(event)
        return event

    def run_in_background(self, label: str, job: Callable[[], Any]) -> bool:
        """Fire a job off the request thread. False if something is already running."""
        with self._claim:
            if self._busy or self._claimed:
                return False
            self._claimed = True
        try:
            threading.Thread(
                target=job, name=label.replace(" ", "-"), daemon=True
            ).start()
        except RuntimeError:
            self._claimed = False
            raise
        return True
