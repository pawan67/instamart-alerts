"""What the poller does once the WAF starts saying no.

A blocked pass still costs a browser bootstrap and metered proxy bandwidth, so a
run of them is worth slowing down for — but only mildly, because a retry now
leaves from a different exit IP and the answer can change in minutes rather than
hours. These pin the three halves of the response: widen the gap while it is
failing, say out loud whether the address moved, and never charge a pass that was
skipped rather than attempted to that count.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from instamart_alerts import scheduler
from instamart_alerts.scheduler import (
    FAILURE_BACKOFF,
    MAX_BACKOFF_MINUTES,
    Scheduler,
)
from instamart_alerts.session import _exit_ip, _report_exit_ip


def poller(minutes: int = 15, failures: int = 0) -> Scheduler:
    s = Scheduler()
    s._minutes = minutes
    s._failures = failures
    return s


# ── the gap ──────────────────────────────────────────────────────────
def test_a_healthy_poller_keeps_the_interval_it_was_given():
    assert poller(failures=0)._gap_minutes() == 15


@pytest.mark.parametrize("failures, expected", [(1, 30), (2, 45), (3, 60)])
def test_each_failure_in_a_row_widens_the_gap(failures, expected):
    assert poller(failures=failures)._gap_minutes() == expected


def test_the_backoff_stops_widening_instead_of_running_away():
    """Twenty failures is not twenty doublings — a poller that backs off to next
    week is indistinguishable from one that died."""
    assert poller(failures=20)._gap_minutes() == poller(
        failures=len(FAILURE_BACKOFF)
    )._gap_minutes()


def test_a_long_interval_is_capped_rather_than_multiplied():
    assert poller(minutes=60, failures=3)._gap_minutes() == MAX_BACKOFF_MINUTES


# ── what counts as a failure ─────────────────────────────────────────
def boom() -> None:
    raise RuntimeError("challenge not cleared")


def test_a_failed_scheduled_pass_counts():
    s = poller()
    s.execute("scheduled check", boom, trigger="scheduled")
    assert s._failures == 1


def test_failures_accumulate_across_passes():
    s = poller()
    for _ in range(3):
        s.execute("scheduled check", boom, trigger="scheduled")
    assert s._gap_minutes() == 60


def test_a_manual_run_failing_does_not_push_the_schedule_out():
    """The user pressing the button is not the poller's evidence. Counting it
    would let someone retrying by hand back the schedule off for hours."""
    s = poller()
    s.execute("manual check", boom, trigger="manual")
    assert s._failures == 0


def test_one_success_puts_the_interval_straight_back():
    s = poller(failures=3)
    s.execute("scheduled check", lambda: [], trigger="scheduled")
    assert s._failures == 0
    assert s._gap_minutes() == 15


def test_a_manual_success_clears_it_too():
    """Same evidence — the WAF has let go, so there is nothing left to sit out."""
    s = poller(failures=3)
    s.execute("manual check", lambda: [], trigger="manual")
    assert s._failures == 0


# ── a pass that never ran is not a pass that failed ──────────────────
def test_a_quiet_night_sleeps_to_the_end_of_it_rather_than_ticking(monkeypatch):
    """Waking every quarter hour to log that it is night is not a check, and
    charging those wake-ups to the failure count blamed the interval on passes
    nobody attempted."""
    monkeypatch.setattr(scheduler.config, "load", lambda: SimpleNamespace(quiet_end=6))
    monkeypatch.setattr(
        scheduler.config, "minutes_until_quiet_end", lambda s, n=None: 92.0
    )
    s = poller(failures=3)
    assert s._poll() == 92.0
    assert s._failures == 3  # untouched: nothing was attempted


@pytest.mark.parametrize("area, watches", [("", ["x"]), ("401209", [])])
def test_nothing_to_poll_for_waits_the_plain_interval(monkeypatch, area, watches):
    monkeypatch.setattr(scheduler.config, "in_quiet_hours", lambda s, n=None: False)
    monkeypatch.setattr(
        scheduler.config, "load", lambda: SimpleNamespace(area=area, watchlist_path="w")
    )
    monkeypatch.setattr(
        scheduler.Watchlist,
        "load",
        staticmethod(lambda p: SimpleNamespace(active=watches)),
    )
    s = poller(minutes=15, failures=2)
    assert s._poll() == 15
    assert s._failures == 2


# ── saying why ───────────────────────────────────────────────────────
def test_an_address_that_moved_is_reported_as_the_cause(caplog):
    with caplog.at_level(logging.WARNING):
        _report_exit_ip("1.2.3.4", "5.6.7.8")
    assert "1.2.3.4" in caplog.text and "5.6.7.8" in caplog.text
    assert "sticky" in caplog.text.lower()


def test_an_address_that_held_still_rules_the_proxy_out(caplog):
    """Worth logging even though nothing is wrong: it is the evidence that the
    next person should stop blaming the proxy."""
    with caplog.at_level(logging.INFO):
        _report_exit_ip("1.2.3.4", "1.2.3.4")
    assert "1.2.3.4" in caplog.text
    assert "moved" not in caplog.text


@pytest.mark.parametrize("before, after", [("", "5.6.7.8"), ("1.2.3.4", ""), ("", "")])
def test_an_unreadable_probe_accuses_nobody(caplog, before, after):
    with caplog.at_level(logging.INFO):
        _report_exit_ip(before, after)
    assert caplog.text == ""


# ── reading the address at all ───────────────────────────────────────
class FakeProbePage:
    def __init__(self, body: str | None = None, boom: bool = False) -> None:
        self._body, self._boom = body, boom
        self.closed = False

    def goto(self, url, **kw):  # noqa: ANN001, ANN201 — a stand-in for Playwright
        if self._boom:
            raise RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")
        return None if self._body is None else type(
            "R", (), {"text": lambda _self: self._body}
        )()

    def close(self) -> None:
        self.closed = True


class FakeCtx:
    def __init__(self, page) -> None:  # noqa: ANN001
        self.page = page

    def new_page(self):  # noqa: ANN201
        return self.page


def test_the_probe_reads_the_address_and_tidies_up():
    page = FakeProbePage(" 1.2.3.4\n")
    assert _exit_ip(FakeCtx(page)) == "1.2.3.4"
    assert page.closed is True


@pytest.mark.parametrize("page", [FakeProbePage(boom=True), FakeProbePage(None)])
def test_a_probe_that_cannot_answer_is_not_an_error(page):
    """The probe exists to explain a failing bootstrap. It must never become
    one — a proxy too broken to reach ipify is exactly when it gets called."""
    assert _exit_ip(FakeCtx(page)) == ""
    assert page.closed is True
