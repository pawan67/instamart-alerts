"""What a single watch failing does to the rest of the pass.

A watch whose search comes back nonsense should not take the others down. A
watch whose *connection* died is a different thing — nothing reached Swiggy, so
the right move is to redial, not to record the watch as broken and carry on with
a hole in the results.
"""

from __future__ import annotations

import httpx
import pytest

from instamart_alerts import runner
from instamart_alerts.config import Settings
from instamart_alerts.session import Blocked, SessionData
from instamart_alerts.watchlist import Watch, Watchlist


def settings(tmp_path) -> Settings:
    return Settings(
        bot_token="t",
        chat_id="1",
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
    )


def two_watches() -> Watchlist:
    return Watchlist(
        [
            Watch(name="A", query="a", min_discount_pct=50),
            Watch(name="B", query="b", min_discount_pct=50),
        ]
    )


def run_with(monkeypatch, tmp_path, search_impl):
    monkeypatch.setattr(runner, "search", search_impl)
    monkeypatch.setattr(runner, "ensure_location", lambda *a: False)
    return runner._run(
        settings(tmp_path),
        two_watches(),
        client=object(),
        data=SessionData(store_id="1", area_label="401209"),
        dry_run=True,
        cooldown_hours=24.0,
    )


def test_a_bad_payload_fails_one_watch_and_spares_the_other(monkeypatch, tmp_path):
    def search(client, store_id, query):
        if query == "a":
            raise KeyError("cards")
        return []

    results = run_with(monkeypatch, tmp_path, search)
    assert [r.watch.name for r in results] == ["A", "B"]
    assert results[0].error and not results[1].error


def test_a_dead_tunnel_aborts_the_pass_instead_of_dropping_a_watch(
    monkeypatch, tmp_path
):
    seen = []

    def search(client, store_id, query):
        seen.append(query)
        raise httpx.ConnectError("tunnel died")

    with pytest.raises(httpx.ConnectError):
        run_with(monkeypatch, tmp_path, search)
    # It stopped at the first watch rather than marching on through a dead link.
    assert seen == ["a"]


def test_a_socks_failure_aborts_the_pass_too(monkeypatch, tmp_path):
    from socksio.exceptions import ProtocolError

    def search(client, store_id, query):
        raise ProtocolError("Malformed reply")

    with pytest.raises(ProtocolError):
        run_with(monkeypatch, tmp_path, search)


def test_a_block_still_aborts_the_pass(monkeypatch, tmp_path):
    def search(client, store_id, query):
        raise Blocked("HTTP 202")

    with pytest.raises(Blocked):
        run_with(monkeypatch, tmp_path, search)
