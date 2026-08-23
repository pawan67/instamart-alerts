"""Mini App API: authentication gating and settings round-trips.

Endpoints that reach Instamart are not exercised here — they need the network.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from instamart_alerts.server import create_app
from tests.test_webauth import TOKEN, UID, init_data


@pytest.fixture
def env(tmp_path, monkeypatch):
    wl = tmp_path / "watchlist.json"
    wl.write_text(
        json.dumps(
            {
                "watches": [
                    {
                        "name": "Eggs",
                        "query": "eggs",
                        "min_discount_pct": 65,
                        "categories": ["Eggs"],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("IM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IM_WATCHLIST", str(wl))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(UID))
    monkeypatch.setenv("IM_AREA", "401209")
    monkeypatch.setenv("IM_WEB_DEV", "0")
    return {"watchlist": wl, "data": tmp_path / "data"}


@pytest.fixture
def client(env):
    return TestClient(create_app())


def auth() -> dict[str, str]:
    return {"X-Init-Data": init_data()}


def test_state_requires_auth(client):
    assert client.get("/api/state").status_code == 401


def test_saving_watches_requires_auth(client):
    r = client.put("/api/watches", json={"watches": []})
    assert r.status_code == 401


def test_settings_requires_auth(client):
    assert client.put("/api/settings", json={"area": "560001"}).status_code == 401


def test_forged_init_data_is_rejected(client):
    r = client.get("/api/state", headers={"X-Init-Data": "user=%7B%22id%22%3A1%7D&hash=deadbeef"})
    assert r.status_code == 401


def test_state_returns_watches(client):
    r = client.get("/api/state", headers=auth())
    assert r.status_code == 200
    body = r.json()
    assert body["area"] == "401209"
    assert body["watches"][0]["query"] == "eggs"
    assert body["telegram_ready"] is True
    assert body["dev_mode"] is False


def test_watches_round_trip(client, env):
    new = {
        "watches": [
            {
                "name": "Milk",
                "query": "milk",
                "min_discount_pct": 40,
                "categories": ["Milk"],
                "exclude": [],
                "include": [],
                "max_price": 99,
                "in_stock_only": True,
                "enabled": True,
            }
        ]
    }
    assert client.put("/api/watches", json=new, headers=auth()).json() == {"saved": 1}

    saved = json.loads(env["watchlist"].read_text())["watches"]
    assert saved[0]["query"] == "milk"
    assert saved[0]["max_price"] == 99
    assert client.get("/api/state", headers=auth()).json()["watches"][0]["name"] == "Milk"


def test_watch_name_defaults_are_preserved(client, env):
    body = {
        "watches": [{"name": "", "query": "curd", "min_discount_pct": 10}]
    }
    client.put("/api/watches", json=body, headers=auth())
    assert json.loads(env["watchlist"].read_text())["watches"][0]["name"] == "curd"


def test_invalid_discount_is_rejected(client):
    body = {"watches": [{"query": "eggs", "min_discount_pct": 150}]}
    assert client.put("/api/watches", json=body, headers=auth()).status_code == 422


def test_empty_query_is_rejected(client):
    body = {"watches": [{"query": "", "min_discount_pct": 10}]}
    assert client.put("/api/watches", json=body, headers=auth()).status_code == 422


def test_changing_area_unpins_the_store(client, env):
    # A stale store id from the previous area must not survive the change.
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "session.json").write_text(
        json.dumps({"cookies": {"aws-waf-token": "x"}, "store_id": "1404876",
                    "area_label": "401209", "device_id": "d"})
    )

    r = client.put("/api/settings", json={"area": "560001"}, headers=auth())
    assert r.json() == {"area": "560001"}

    session = json.loads((env["data"] / "session.json").read_text())
    assert session["store_id"] == ""
    assert session["cookies"] == {"aws-waf-token": "x"}  # token is still reusable
    assert client.get("/api/state", headers=auth()).json()["area"] == "560001"


def test_blank_area_is_rejected(client):
    assert client.put("/api/settings", json={"area": ""}, headers=auth()).status_code == 422


def test_dev_mode_skips_auth(client, monkeypatch):
    monkeypatch.setenv("IM_WEB_DEV", "1")
    r = client.get("/api/state")
    assert r.status_code == 200
    assert r.json()["dev_mode"] is True


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "telegram-web-app.js" in r.text
