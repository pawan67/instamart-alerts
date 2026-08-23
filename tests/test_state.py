"""De-duplication rules: alert on something new, stay quiet otherwise."""

from __future__ import annotations

import time

import pytest

from instamart_alerts.instamart import Product
from instamart_alerts.state import AlertState


def make_product(price: float = 100.0, mrp: float = 300.0, sku: str = "SKU1") -> Product:
    return Product(
        sku_id=sku,
        product_id="P1",
        name="Test Eggs",
        brand="Test",
        quantity="6 Pieces",
        category="Eggs",
        sub_category="White Eggs",
        mrp=mrp,
        price=price,
        discount_pct=round((mrp - price) / mrp * 100, 1),
        in_stock=True,
        unit_price="",
        offer_label="",
    )


@pytest.fixture
def state(tmp_path):
    return AlertState.load(tmp_path / "alerts.json", cooldown_hours=24.0)


def test_first_sighting_alerts(state):
    assert state.should_alert("Eggs", make_product())


def test_same_price_stays_quiet(state):
    p = make_product()
    state.record("Eggs", p, hit=True)
    assert not state.should_alert("Eggs", p)


def test_deeper_cut_realerts(state):
    state.record("Eggs", make_product(price=100.0), hit=True)
    assert state.should_alert("Eggs", make_product(price=90.0))


def test_price_creeping_up_stays_quiet(state):
    state.record("Eggs", make_product(price=100.0), hit=True)
    assert not state.should_alert("Eggs", make_product(price=105.0))


def test_deal_lapsing_then_returning_realerts(state):
    # Seen below threshold last pass...
    state.record("Eggs", make_product(price=280.0), hit=False)
    # ...and now it qualifies again.
    assert state.should_alert("Eggs", make_product(price=100.0))


def test_cooldown_expiry_realerts(state):
    p = make_product()
    state.record("Eggs", p, hit=True)
    key = f"Eggs::{p.sku_id}"
    state.seen[key]["ts"] = time.time() - 25 * 3600
    assert state.should_alert("Eggs", p)


def test_watches_are_scoped_independently(state):
    p = make_product()
    state.record("Eggs", p, hit=True)
    assert state.should_alert("Other watch", p)


def test_state_round_trips(tmp_path):
    path = tmp_path / "alerts.json"
    s = AlertState.load(path)
    s.record("Eggs", make_product(), hit=True)
    s.save()
    assert not AlertState.load(path).should_alert("Eggs", make_product())


def test_corrupt_state_file_resets(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text("{not json")
    assert AlertState.load(path).seen == {}


def test_prune_drops_only_stale_absent_skus(state):
    old, live = make_product(sku="OLD"), make_product(sku="LIVE")
    state.record("Eggs", old, hit=True)
    state.record("Eggs", live, hit=True)
    state.seen["Eggs::OLD"]["ts"] = time.time() - 40 * 86400
    state.prune({"Eggs::LIVE"})
    assert "Eggs::OLD" not in state.seen
    assert "Eggs::LIVE" in state.seen
