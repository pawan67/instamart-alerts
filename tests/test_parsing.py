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
    if "allowed" in kw:
        v["cartAllowedQuantity"] = {"allowedQuantity": kw["allowed"]}
    if kw.get("no_inventory"):
        del v["inventory"]
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


def test_parent_in_stock_flag_never_rescues_a_sold_out_variant(caplog):
    """The item-level flag is true whenever *any* variant is available.

    A sold-out 12-pack sitting under an in-stock single carries inStock=true on
    the item, so reading it as a fallback used to send alerts for packs nobody
    could buy. With no per-variant signal the variant is out of stock, full stop.
    """
    c = client_returning(payload(variation("S1", "100", "20", no_inventory=True)))
    (p,) = search(c, "1", "eggs")
    assert not p.in_stock
    assert "no inventory.inStock" in caplog.text


def test_zero_cart_allowance_is_out_of_stock():
    """Swiggy leaves inStock true on variants it will not let you add to a cart."""
    c = client_returning(payload(variation("S1", "100", "20", allowed=0)))
    (p,) = search(c, "1", "eggs")
    assert not p.in_stock


def test_a_normal_cart_limit_is_still_in_stock():
    c = client_returning(payload(variation("S1", "100", "20", allowed=2)))
    (p,) = search(c, "1", "eggs")
    assert p.in_stock


def test_empty_feed_returns_nothing():
    c = client_returning({"data": {"cards": []}})
    assert search(c, "1", "eggs") == []


@pytest.mark.parametrize("status", [202, 403])
def test_waf_refusal_raises_blocked(status):
    c = client_returning({}, status=status)
    with pytest.raises(Blocked):
        request(c, "GET", f"{instamart.API}/search/v2")


# ── pagination ────────────────────────────────────────────────────────────────
# Search serves ~32 products a page and keeps the rest behind a cursor. Reading
# only page one hid two thirds of the "yogurt" aisle at store 1394450.


def paged(*pages: dict) -> httpx.Client:
    """A client that serves `pages` in order, keyed off the offset it is asked for."""
    calls: list[tuple[int, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        offset = int(dict(request.url.params).get("offset", 0))
        body = _json.loads(request.content or b"{}")
        calls.append((offset, body.get("search_results_offset", "")))
        if offset >= len(pages):
            return httpx.Response(200, json={"data": {"cards": []}})
        return httpx.Response(200, json=pages[offset])

    c = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://www.swiggy.com"
    )
    c.calls = calls  # type: ignore[attr-defined]
    return c


def cursor(page: dict, next_offset: str | None, results_offset: str = "32") -> dict:
    page["data"]["pageOffset"] = {"nextOffset": next_offset, "widgetOffset": {}}
    page["data"]["searchResultsOffset"] = results_offset
    return page


def test_follows_the_cursor_across_pages():
    c = paged(
        cursor(payload(variation("S1", "100", "20")), "1", "32"),
        cursor(payload(variation("S2", "100", "30")), "2", "75"),
        cursor(payload(variation("S3", "100", "40")), None),
    )
    assert [p.sku_id for p in search(c, "1", "yogurt")] == ["S1", "S2", "S3"]
    # Both halves of the cursor go back out: the page number and the running
    # product count. Sending the number alone re-serves page one.
    assert c.calls == [(0, "0"), (1, "32"), (2, "75")]  # type: ignore[attr-defined]


def test_a_single_page_search_asks_for_nothing_more():
    c = paged(payload(variation("S1", "100", "20")))
    assert len(search(c, "1", "eggs")) == 1
    assert len(c.calls) == 1  # type: ignore[attr-defined]


def test_a_cursor_that_does_not_advance_stops_the_walk():
    """A next-offset pointing at the page just read would loop to the cap."""
    c = paged(cursor(payload(variation("S1", "100", "20")), "0"))
    assert len(search(c, "1", "eggs")) == 1
    assert len(c.calls) == 1  # type: ignore[attr-defined]


def test_duplicate_skus_across_pages_are_counted_once():
    c = paged(
        cursor(payload(variation("S1", "100", "20")), "1"),
        cursor(
            payload(variation("S1", "100", "20"), variation("S2", "100", "30")), None
        ),
    )
    assert [p.sku_id for p in search(c, "1", "yogurt")] == ["S1", "S2"]


def test_the_page_cap_is_announced_not_swallowed(caplog):
    """A truncated read must not look like a complete one."""

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params).get("offset", 0))
        return httpx.Response(
            200,
            json=cursor(payload(variation(f"S{offset}", "100", "20")), str(offset + 1)),
        )

    c = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://www.swiggy.com"
    )
    with caplog.at_level("WARNING"):
        products = search(c, "1", "yogurt")
    assert len(products) == instamart.MAX_SEARCH_PAGES
    assert "more than" in caplog.text
