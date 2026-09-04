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
from instamart_alerts.instamart import Product
from instamart_alerts.session import Blocked, SessionData
from instamart_alerts.watchlist import Watch, Watchlist


def product(discount_pct: float) -> Product:
    return Product(
        sku_id="s1",
        product_id="p1",
        name="Eggs",
        brand="b",
        quantity="6 pcs",
        category="c",
        sub_category="sc",
        mrp=100.0,
        price=100.0 - discount_pct,
        discount_pct=discount_pct,
        in_stock=True,
        unit_price="",
        offer_label="",
    )


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


def test_every_watch_empty_at_once_is_treated_as_a_refusal(monkeypatch, tmp_path):
    """Not what a live store looks like — what a soured token looks like."""
    results = pytest.raises(
        runner.EmptyPass, run_with, monkeypatch, tmp_path, lambda *a: []
    ).value.results
    # Carried, so the ladder's last attempt can hand them back rather than
    # throw away a pass that may simply have found nothing.
    assert [r.watch.name for r in results] == ["A", "B"]


def test_one_watch_finding_something_makes_the_pass_a_real_answer(
    monkeypatch, tmp_path
):
    def search(client, store_id, query):
        return [] if query == "a" else [product(30)]

    results = run_with(monkeypatch, tmp_path, search)
    assert [len(r.candidates) for r in results] == [0, 1]


def test_a_watch_that_errored_leaves_the_pass_no_opinion_to_offer(
    monkeypatch, tmp_path
):
    """One broken watch is its own reported problem, not evidence of a block."""

    def search(client, store_id, query):
        if query == "a":
            raise KeyError("cards")
        return []

    results = run_with(monkeypatch, tmp_path, search)
    assert results[0].error and not results[1].error


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
