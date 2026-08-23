"""Parsing of the Instamart search payload, pinned to a real captured response shape."""

from __future__ import annotations

import httpx
import pytest

from instamart_alerts import instamart
from instamart_alerts.instamart import _money, search
from instamart_alerts.session import Blocked, request


def money(units: str, nanos: int = 0) -> dict:
    return {"currencyCode": "INR", "units": units, "nanos": nanos}


def variation(sku: str, mrp: str, offer: str, **kw) -> dict:
    v = {
        "skuId": sku,
        "displayName": kw.get("name", "Farm Eggs"),
        "brandName": "Farm",
        "quantityDescription": "6 Pieces",
        "category": kw.get("category", "Eggs"),
        "subCategoryType": "White Eggs",
        "inventory": {"inStock": kw.get("in_stock", True), "lowStockText": ""},
        "price": {
            "mrp": money(mrp),
            "offerPrice": money(offer),
            "unitLevelPrice": "9.2/piece",
            "offerApplied": {"listingDescription": kw.get("label", "24% OFF")},
        },
    }
    if kw.get("no_price"):
        v["price"] = {}
    return v


def payload(*variations: dict) -> dict:
    return {
        "statusCode": 0,
        "data": {
            "cards": [
                # A non-product card — the real feed is full of these.
                {"card": {"card": {"@type": "...InlineViewFilterSortWidget"}}},
                {
                    "card": {
                        "card": {
                            "@type": "...GridWidget",
                            "gridElements": {
                                "infoWithStyle": {
                                    "items": [
                                        {
                                            "productId": "PROD1",
                                            "brand": "Farm",
                                            "inStock": True,
                                            "variations": list(variations),
                                        }
                                    ]
                                }
                            },
                        }
                    }
                },
            ]
        },
    }


def client_returning(obj: dict, status: int = 200) -> httpx.Client:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=obj))
    return httpx.Client(transport=transport, base_url="https://www.swiggy.com")


def test_money_units_and_nanos():
    assert _money(money("290")) == 290.0
    assert _money(money("290", 500_000_000)) == pytest.approx(290.5)
    assert _money(None) is None


def test_parses_price_and_discount():
    c = client_returning(payload(variation("S1", "290", "220")))
    (p,) = search(c, "1062419", "eggs")
    assert (p.sku_id, p.mrp, p.price) == ("S1", 290.0, 220.0)
    assert p.discount_pct == 24.1
    assert p.product_id == "PROD1"
    assert p.category == "Eggs"
    assert p.in_stock
    assert p.url == "https://www.swiggy.com/instamart/item/PROD1"


def test_zero_discount_is_not_an_error():
    c = client_returning(payload(variation("S1", "120", "120", label="")))
    (p,) = search(c, "1", "eggs")
    assert p.discount_pct == 0.0


def test_variant_without_price_is_skipped():
    c = client_returning(payload(variation("S1", "0", "0", no_price=True)))
    assert search(c, "1", "eggs") == []


def test_duplicate_skus_collapse():
    c = client_returning(payload(variation("S1", "100", "50"), variation("S1", "100", "50")))
    assert len(search(c, "1", "eggs")) == 1


def test_out_of_stock_flag_read_from_inventory():
    c = client_returning(payload(variation("S1", "100", "50", in_stock=False)))
    (p,) = search(c, "1", "eggs")
    assert not p.in_stock


def test_empty_feed_returns_nothing():
    c = client_returning({"data": {"cards": []}})
    assert search(c, "1", "eggs") == []


@pytest.mark.parametrize("status", [202, 403])
def test_waf_refusal_raises_blocked(status):
    c = client_returning({}, status=status)
    with pytest.raises(Blocked):
        request(c, "GET", f"{instamart.API}/search/v2")
