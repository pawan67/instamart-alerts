"""The overnight pause.

Nothing worth being woken for is discounted at 3am, and every poll costs metered
proxy bandwidth. The window is IST because the store is, and the container is
almost certainly not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from instamart_alerts import config
from instamart_alerts.config import IST, Settings, in_quiet_hours

UTC = timezone.utc


def at(hour: int, minute: int = 0, tz=IST) -> datetime:
    return datetime(2026, 8, 31, hour, minute, tzinfo=tz)


def make(**kw) -> Settings:
    base = dict(
        bot_token="", chat_id="", area="", proxy=None,
        data_dir=config.ROOT, watchlist_path=config.ROOT, headless=True,
    )
    return Settings(**{**base, **kw})


# ── the default window: midnight to 06:00 IST ────────────────────────
@pytest.mark.parametrize(
    "hour, quiet",
    [
        (0, True),    # midnight, the moment it starts
        (3, True),
        (5, True),
        (6, False),   # end is exclusive — 06:00 polls
        (12, False),
        (23, False),  # and it does not start until midnight
    ],
)
def test_the_default_window_is_midnight_to_six(hour, quiet):
    assert in_quiet_hours(make(), at(hour)) is quiet


def test_the_window_is_ist_not_whatever_the_container_thinks():
    """03:00 IST is 21:30 UTC the day before. A container on UTC must still
    pause, and must not pause at 03:00 UTC — which is 08:30 IST."""
    assert in_quiet_hours(make(), at(21, 30, UTC)) is True
    assert in_quiet_hours(make(), at(3, 0, UTC)) is False


# ── the option, which is on by default ───────────────────────────────
def test_it_is_on_by_default():
    assert Settings.quiet_hours is True
    assert (Settings.quiet_start, Settings.quiet_end) == (0, 6)


def test_turning_it_off_polls_through_the_night():
    assert in_quiet_hours(make(quiet_hours=False), at(3)) is False


# ── windows that wrap midnight, and the one that must not lock us out ──
@pytest.mark.parametrize(
    "hour, quiet",
    [(22, True), (23, True), (0, True), (5, True), (6, False), (12, False)],
)
def test_a_window_may_wrap_midnight(hour, quiet):
    assert in_quiet_hours(make(quiet_start=22, quiet_end=6), at(hour)) is quiet


def test_a_zero_length_window_means_never_not_always():
    """Both ends equal is the one setting that could silently stop the poller
    for a whole day, so it reads as 'no quiet hours' instead."""
    for hour in (0, 6, 13, 23):
        assert in_quiet_hours(make(quiet_start=6, quiet_end=6), at(hour)) is False


# ── config parsing ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected", [("0", 0), ("6", 6), ("23", 23), ("24", 9), ("-1", 9),
                      ("half past", 9), (None, 9), ("", 9)]
)
def test_an_out_of_range_hour_is_a_typo_and_falls_back(raw, expected):
    assert config._hour(raw, 9) == expected


# ── what the scheduler actually does with it ─────────────────────────
def test_a_scheduled_poll_is_skipped_during_quiet_hours(monkeypatch):
    from instamart_alerts import scheduler as sched

    quiet = make(area="401209")
    monkeypatch.setattr(sched.config, "load", lambda: quiet)
    monkeypatch.setattr(sched.config, "in_quiet_hours", lambda s, now=None: True)

    ran = []
    s = sched.Scheduler()
    monkeypatch.setattr(s, "execute", lambda *a, **k: ran.append(a))
    s._poll()

    assert ran == []


def test_a_manual_check_still_runs_during_quiet_hours(monkeypatch):
    """The pause is for the poller. Asking for a check is unambiguous, whatever
    the clock says — execute() is what the panel's buttons call, and it has no
    opinion about the hour."""
    from instamart_alerts import scheduler as sched

    monkeypatch.setattr(sched.config, "in_quiet_hours", lambda s, now=None: True)
    s = sched.Scheduler()
    out = s.execute("manual check", lambda: [], trigger="manual")

    assert out is not None
    assert "quiet" not in str(out).lower()
