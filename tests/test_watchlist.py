"""Watch filtering: keep real eggs, drop the padding Instamart mixes in."""

from __future__ import annotations

import json

import pytest

from instamart_alerts.instamart import Product
from instamart_alerts.watchlist import Watch, Watchlist


def product(**kw) -> Product:
    base = dict(
        sku_id="S",
        product_id="P",
        name="Farm Eggs",
        brand="Farm",
        quantity="6 Pieces",
        category="Eggs",
        sub_category="White Eggs",
        mrp=100.0,
        price=30.0,
        discount_pct=70.0,
        in_stock=True,
        unit_price="",
        offer_label="",
    )
    base.update(kw)
    return Product(**base)


@pytest.fixture
def eggs() -> Watch:
    return Watch(
        name="Eggs",
        query="eggs",
        min_discount_pct=65,
        categories=("Eggs",),
        exclude=("batter", "paneer"),
    )


def test_hit_above_threshold(eggs):
    assert eggs.is_hit(product(discount_pct=70.0))


def test_miss_below_threshold(eggs):
    assert not eggs.is_hit(product(discount_pct=64.9))


def test_threshold_is_inclusive(eggs):
    assert eggs.is_hit(product(discount_pct=65.0))


def test_other_category_filtered_out(eggs):
    # Instamart pads egg searches with things like idli batter.
    p = product(name="NOICE Idli Dosa Batter", category="Batters and Chutneys")
    assert not eggs.matches(p)
    assert not eggs.is_hit(p)


def test_excluded_name_filtered_out(eggs):
    assert not eggs.matches(product(name="NOICE Fresh Malai Paneer"))


def test_out_of_stock_filtered_out(eggs):
    assert not eggs.is_hit(product(in_stock=False))


def test_in_stock_only_can_be_disabled():
    w = Watch(name="e", query="eggs", min_discount_pct=10, in_stock_only=False)
    assert w.is_hit(product(in_stock=False))


def test_include_requires_a_match():
    w = Watch(name="e", query="eggs", min_discount_pct=10, include=("brown",))
    assert not w.matches(product(name="White Eggs"))
    assert w.matches(product(name="Brown Eggs"))


def test_max_price_caps_hits():
    w = Watch(name="e", query="eggs", min_discount_pct=10, max_price=50.0)
    assert w.is_hit(product(price=40.0))
    assert not w.is_hit(product(price=60.0))


def test_sub_category_also_satisfies_category_filter():
    w = Watch(name="e", query="eggs", min_discount_pct=10, categories=("white eggs",))
    assert w.matches(product(category="Dairy", sub_category="White Eggs"))


def test_loads_shipped_watchlist(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(
        json.dumps(
            {
                "watches": [
                    {"query": "eggs", "min_discount_pct": 65, "categories": ["Eggs"]},
                    {"name": "off", "query": "milk", "min_discount_pct": 50, "enabled": False},
                ]
            }
        )
    )
    wl = Watchlist.load(p)
    assert len(wl.watches) == 2
    assert [w.name for w in wl.active] == ["eggs"]
    assert wl.watches[0].min_discount_pct == 65


def test_loads_bare_list_form(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps([{"query": "eggs", "min_discount_pct": 40}]))
    assert Watchlist.load(p).active[0].query == "eggs"
