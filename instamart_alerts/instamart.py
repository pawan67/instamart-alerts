"""Swiggy Instamart catalogue reads: geocode an area, pin the dark store, search.

Endpoints (all under https://www.swiggy.com/api/instamart, verified 2026-08-23):

  GET  /maps/suggestions?input=<area or pincode>
       -> data[].{place_id, description}
  GET  /maps/address-widgets/v2?place_id=<id>
       -> data.address.location.{latitude,longitude} + metadata.formattedAddress
  POST /home/select-location/v2   {"data": {lat, lng, address, addressId,
                                            annotation, clientId}}
       -> the home feed for the resolved dark store; the store id is only
          exposed inside `swiggy://...?storeId=<n>` deeplinks in that payload.
  POST /search/v2?storeId=&primaryStoreId=&offset=0&...
       {"facets": [], "sortAttribute": "", "query": ..., "search_results_offset":
        "0", "page_type": "INSTAMART_SEARCH_PAGE", "is_pre_search_tag": false}
       -> data.cards[].card.card.gridElements.infoWithStyle.items[].variations[]

Prices are Google-style Money: `units` (string rupees) + `nanos` (1e-9 rupee).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from .config import API
from .session import SessionData, request

log = logging.getLogger(__name__)

STORE_ID_RE = re.compile(r"storeId=(\d+)")


@dataclass(frozen=True)
class Product:
    sku_id: str
    product_id: str
    name: str
    brand: str
    quantity: str
    category: str
    sub_category: str
    mrp: float
    price: float
    discount_pct: float
    in_stock: bool
    unit_price: str
    offer_label: str

    @property
    def url(self) -> str:
        return f"https://www.swiggy.com/instamart/item/{self.product_id}"

    def __str__(self) -> str:
        return f"{self.name} [{self.quantity}] {self.discount_pct}% off ₹{self.price:g}"


def _money(m: dict[str, Any] | None) -> float | None:
    """Google Money -> rupees."""
    if not m:
        return None
    try:
        return int(m.get("units") or 0) + (m.get("nanos") or 0) / 1e9
    except (TypeError, ValueError):
        return None


def geocode(client: httpx.Client, area: str) -> dict[str, Any]:
    """Resolve a free-text area/pincode to coordinates + a formatted address."""
    r = request(client, "GET", f"{API}/maps/suggestions", params={"input": area})
    preds = (r.json() or {}).get("data") or []
    if not preds:
        raise LookupError(f"no Swiggy location match for {area!r}")

    place_id = preds[0]["place_id"]
    r = request(
        client, "GET", f"{API}/maps/address-widgets/v2", params={"place_id": place_id}
    )
    addr = ((r.json() or {}).get("data") or {}).get("address") or {}
    loc = addr.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        raise LookupError(f"Swiggy returned no coordinates for {area!r}")

    formatted = (addr.get("metadata") or {}).get("formattedAddress") or addr.get(
        "subtitle", ""
    )
    return {
        "lat": float(lat),
        "lng": float(lng),
        "address": formatted,
        "title": addr.get("title") or preds[0].get("description", area),
        "matched": preds[0].get("description", ""),
    }


def select_store(client: httpx.Client, place: dict[str, Any]) -> str:
    """Pin the session to the dark store serving these coordinates."""
    payload = {
        "data": {
            "lat": place["lat"],
            "lng": place["lng"],
            "address": place["address"],
            "addressId": "",
            "annotation": place["address"],
            "clientId": "INSTAMART-APP",
        }
    }
    r = request(client, "POST", f"{API}/home/select-location/v2", json=payload)

    # The store id is not a field anywhere — it is only embedded in the
    # deeplinks of the returned home feed, so take the most common one.
    ids = STORE_ID_RE.findall(r.text)
    if not ids:
        raise LookupError(
            "select-location returned no storeId — Instamart may not serve this area"
        )
    store_id = Counter(ids).most_common(1)[0][0]

    client.cookies.set(
        "userLocation",
        urllib.parse.quote(
            json.dumps(
                {
                    "lat": place["lat"],
                    "lng": place["lng"],
                    "address": place["address"],
                    "area": place["title"],
                    "id": "",
                    "annotation": place["address"],
                }
            ),
            safe="",
        ),
        domain=".swiggy.com",
    )
    return store_id


def ensure_location(client: httpx.Client, data: SessionData, area: str) -> bool:
    """Make sure the session points at the store for `area`. Returns True if changed."""
    if data.store_id and data.area_label == area:
        # Re-apply the cookie; a rebuilt client starts without it.
        if data.lat is not None and data.lng is not None:
            client.cookies.set(
                "userLocation",
                urllib.parse.quote(
                    json.dumps(
                        {
                            "lat": data.lat,
                            "lng": data.lng,
                            "address": data.area_label,
                            "area": data.area_label,
                            "id": "",
                            "annotation": data.area_label,
                        }
                    ),
                    safe="",
                ),
                domain=".swiggy.com",
            )
        return False

    place = geocode(client, area)
    store_id = select_store(client, place)
    log.info("area %r -> %s (store %s)", area, place["matched"], store_id)
    data.store_id = store_id
    data.area_label = area
    data.lat, data.lng = place["lat"], place["lng"]
    return True


def _in_stock(variation: dict[str, Any]) -> bool | None:
    """Is this exact variant buyable right now? None when Swiggy says nothing.

    Only `variation.inventory.inStock` tracks the variant. The item-level
    `inStock` sitting one level up is the parent product's flag — true when
    *any* of its variants is available — so a sold-out 12-pack under an
    in-stock single reads as in stock there. Measured 2026-09-01 over 485
    variants: all 36 out-of-stock ones had item.inStock == true.

    `cartAllowedQuantity.allowedQuantity == 0` is the same verdict from the
    other side — nothing can be added to a cart — and is checked as a backstop
    for the day `inventory` starts lying.
    """
    inventory = variation.get("inventory")
    if not isinstance(inventory, dict) or "inStock" not in inventory:
        return None
    if not inventory["inStock"]:
        return False
    allowed = (variation.get("cartAllowedQuantity") or {}).get("allowedQuantity")
    return allowed != 0


def _iter_variations(payload: dict[str, Any]):
    for card in ((payload.get("data") or {}).get("cards") or []):
        inner = (card.get("card") or {}).get("card") or {}
        grid = (inner.get("gridElements") or {}).get("infoWithStyle") or {}
        for item in grid.get("items") or []:
            for variation in item.get("variations") or []:
                yield item, variation


def search(client: httpx.Client, store_id: str, query: str) -> list[Product]:
    """Run an Instamart search and flatten it to one row per purchasable variant."""
    r = request(
        client,
        "POST",
        f"{API}/search/v2",
        params={
            "offset": 0,
            "ageConsent": "false",
            "voiceSearchTrackingId": "",
            "storeId": store_id,
            "primaryStoreId": store_id,
            "secondaryStoreId": "",
        },
        json={
            "facets": [],
            "sortAttribute": "",
            "query": query,
            "search_results_offset": "0",
            "page_type": "INSTAMART_SEARCH_PAGE",
            "is_pre_search_tag": False,
        },
    )

    out: list[Product] = []
    seen: set[str] = set()
    unknown_stock = 0
    for item, v in _iter_variations(r.json()):
        price_block = v.get("price") or {}
        mrp = _money(price_block.get("mrp"))
        offer = _money(price_block.get("offerPrice"))
        if not mrp or mrp <= 0 or offer is None:
            continue

        sku = v.get("skuId") or ""
        if sku in seen:
            continue
        seen.add(sku)

        # No stock signal means no alert. Guessing "available" here is how
        # sold-out packs ended up in alerts, and a missed deal is the cheaper
        # mistake of the two.
        in_stock = _in_stock(v)
        if in_stock is None:
            unknown_stock += 1
            in_stock = False

        out.append(
            Product(
                sku_id=sku,
                product_id=item.get("productId") or "",
                name=v.get("displayName") or item.get("displayName") or "",
                brand=v.get("brandName") or item.get("brand") or "",
                quantity=v.get("quantityDescription") or "",
                category=v.get("category") or "",
                sub_category=v.get("subCategoryType") or "",
                mrp=mrp,
                price=offer,
                discount_pct=round((mrp - offer) / mrp * 100, 1),
                in_stock=in_stock,
                unit_price=price_block.get("unitLevelPrice") or "",
                offer_label=(price_block.get("offerApplied") or {}).get(
                    "listingDescription", ""
                ),
            )
        )

    if unknown_stock:
        log.warning(
            "%d of %d variants for %r carried no inventory.inStock — treating "
            "them as out of stock; the field may have been renamed",
            unknown_stock,
            len(out),
            query,
        )
    return out
