"""Fan-out delivery: who gets the message, and what counts as a success.

`send()` returning True is what lets the runner keep its de-duplication record.
Getting that wrong either re-sends every pass or silently drops alerts, so the
partial-failure rule is pinned down here.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from instamart_alerts.config import Settings, parse_chat_ids
from instamart_alerts.notify import format_alert, send
from instamart_alerts.instamart import Product


def settings(chat_id: str, tmp_path: Path) -> Settings:
    return Settings(
        bot_token="1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        chat_id=chat_id,
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
    )


@pytest.fixture
def telegram(monkeypatch):
    """Records every sendMessage, and can be told which chats to reject."""
    calls: list[dict] = []
    rejects: set[str] = set()

    def fake_post(url, *, json, timeout, proxy):
        calls.append(json)
        chat = str(json["chat_id"])
        if chat in rejects:
            return httpx.Response(400, text='{"description":"chat not found"}')
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr(httpx, "post", fake_post)
    return {"calls": calls, "rejects": rejects}


def test_every_recipient_gets_the_message(telegram, tmp_path):
    assert send(settings("111, 222, 333", tmp_path), "hello") is True
    assert [c["chat_id"] for c in telegram["calls"]] == ["111", "222", "333"]
    assert {c["text"] for c in telegram["calls"]} == {"hello"}


def test_a_single_recipient_still_works(telegram, tmp_path):
    assert send(settings("111", tmp_path), "hello") is True
    assert len(telegram["calls"]) == 1


def test_a_partial_failure_still_counts_as_delivered(telegram, tmp_path):
    """Otherwise one bad id re-sends to the healthy chats on every pass."""
    telegram["rejects"].add("222")
    assert send(settings("111, 222", tmp_path), "hello") is True
    assert len(telegram["calls"]) == 2  # the bad id did not stop the good one


def test_a_rejected_chat_does_not_block_the_ones_after_it(telegram, tmp_path):
    telegram["rejects"].add("111")
    assert send(settings("111, 222, 333", tmp_path), "hello") is True
    assert [c["chat_id"] for c in telegram["calls"]] == ["111", "222", "333"]


def test_total_failure_is_reported(telegram, tmp_path):
    telegram["rejects"].update({"111", "222"})
    assert send(settings("111, 222", tmp_path), "hello") is False


def test_a_transport_error_on_one_chat_is_survivable(monkeypatch, tmp_path):
    seen: list[str] = []

    def flaky(url, *, json, timeout, proxy):
        seen.append(str(json["chat_id"]))
        if json["chat_id"] == "111":
            raise httpx.ConnectError("tunnel died")
        return httpx.Response(200, text='{"ok":true}')

    monkeypatch.setattr(httpx, "post", flaky)
    assert send(settings("111, 222", tmp_path), "hello") is True
    assert seen == ["111", "222"]


def test_nothing_is_sent_without_a_recipient(telegram, tmp_path):
    assert send(settings("", tmp_path), "hello") is False
    assert telegram["calls"] == []


def test_a_long_message_is_truncated_once_for_everyone(telegram, tmp_path):
    body = "\n".join(f"line {i}" for i in range(2000))
    assert send(settings("111, 222", tmp_path), body) is True
    texts = {c["text"] for c in telegram["calls"]}
    assert len(texts) == 1  # the same truncated body, not two different ones
    assert len(texts.pop()) <= 4096


def test_the_alert_body_is_built_once_and_shared(telegram, tmp_path):
    product = Product(
        sku_id="s1",
        product_id="p1",
        name="NOICE High Protein Eggs",
        brand="NOICE",
        quantity="6 Pieces",
        category="Eggs",
        sub_category="Eggs",
        mrp=180.0,
        price=120.0,
        discount_pct=33.3,
        in_stock=True,
        unit_price="₹20/pc",
        offer_label="",
    )
    text = format_alert("Eggs", "401209", [product])
    send(settings("111, 222", tmp_path), text)
    assert all("NOICE High Protein Eggs" in c["text"] for c in telegram["calls"])


# ── parsing ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("111", ("111",)),
        ("111,222", ("111", "222")),
        ("111, 222", ("111", "222")),
        ("111 222", ("111", "222")),
        ("111\n222", ("111", "222")),
        ("111; 222", ("111", "222")),
        ("  111  ,, 222 ", ("111", "222")),
        ("111,111,222", ("111", "222")),
        (["111", " 222 "], ("111", "222")),
        ("-1001234567890", ("-1001234567890",)),
        ("", ()),
        (None, ()),
    ],
)
def test_recipient_parsing(raw, expected):
    assert parse_chat_ids(raw) == expected
