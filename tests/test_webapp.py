"""Standalone control panel: auth gating, settings round-trips, watch saves.

Endpoints that reach Instamart or Telegram are not exercised — they need the
network. What is covered is everything that decides whether a request is allowed
and whether what it writes survives a reload.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from instamart_alerts import config, webapp

TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


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
    data = tmp_path / "data"
    monkeypatch.setenv("IM_DATA_DIR", str(data))
    monkeypatch.setenv("IM_WATCHLIST", str(wl))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("IM_AREA", "401209")
    monkeypatch.delenv("IM_WEB_PASSWORD", raising=False)
    return {"watchlist": wl, "data": data}


@pytest.fixture
def client(env):
    with TestClient(webapp.create_app()) as c:
        yield c
    webapp.scheduler.stop()


# ── auth ─────────────────────────────────────────────────────────────
def test_loopback_is_trusted_when_no_password_is_set(client):
    assert client.get("/api/bootstrap").status_code == 200


def test_remote_client_is_refused_when_no_password_is_set(env):
    with TestClient(webapp.create_app(), client=("203.0.113.9", 51234)) as c:
        r = c.get("/api/bootstrap")
    assert r.status_code == 403
    assert "localhost" in r.json()["detail"]


def test_password_mode_requires_login(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        assert c.get("/api/bootstrap").status_code == 401
        assert c.get("/api/session").json() == {
            "authed": False,
            "password_required": True,
            "local": True,
        }
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert c.post("/api/login", json={"password": "hunter2"}).status_code == 200
        # The cookie the login set now carries the session.
        assert c.get("/api/bootstrap").status_code == 200
        c.post("/api/logout")
        assert c.get("/api/bootstrap").status_code == 401


def test_a_cookie_signed_with_another_secret_is_rejected(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        c.cookies.set(webapp.COOKIE, "1700000000.deadbeef")
        assert c.get("/api/bootstrap").status_code == 401


def test_event_stream_is_gated(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        assert c.get("/api/events").status_code == 401


# ── settings ─────────────────────────────────────────────────────────
def test_saving_credentials_persists_and_is_masked(client, env):
    r = client.put("/api/settings", json={"bot_token": TOKEN, "chat_id": "958113963"})
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["telegram_ready"] is True
    assert body["chat_id"] == "958113963"
    # The raw token never leaves the server.
    assert TOKEN not in json.dumps(r.json())
    assert body["bot_token_masked"].startswith("1234567890:")
    assert body["bot_token_masked"].endswith(TOKEN[-4:])

    saved = json.loads((env["data"] / "settings.json").read_text())
    assert saved["bot_token"] == TOKEN
    assert config.load().bot_token == TOKEN


def test_posting_the_mask_back_does_not_clobber_the_token(client):
    client.put("/api/settings", json={"bot_token": TOKEN, "chat_id": "1"})
    masked = client.get("/api/bootstrap").json()["settings"]["bot_token_masked"]

    client.put("/api/settings", json={"bot_token": masked, "chat_id": "2"})
    assert config.load().bot_token == TOKEN
    assert config.load().chat_id == "2"


def test_an_empty_token_clears_the_connection(client):
    client.put("/api/settings", json={"bot_token": TOKEN, "chat_id": "1"})
    r = client.put("/api/settings", json={"bot_token": ""})
    assert r.json()["settings"]["telegram_ready"] is False
    assert config.load().bot_token == ""


def test_overrides_win_over_the_environment(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    assert config.load().chat_id == "111"
    client.put("/api/settings", json={"chat_id": "222"})
    assert config.load().chat_id == "222"


# ── multiple recipients ──────────────────────────────────────────────
def test_several_recipients_round_trip(client):
    r = client.put("/api/settings", json={"chat_id": ["111", "222", "-1009876543210"]})
    assert r.status_code == 200
    assert r.json()["settings"]["chat_ids"] == ["111", "222", "-1009876543210"]
    assert config.load().chat_ids == ("111", "222", "-1009876543210")


def test_a_comma_separated_string_is_accepted_too(client):
    client.put("/api/settings", json={"chat_id": "111, 222 333"})
    assert config.load().chat_ids == ("111", "222", "333")


def test_duplicate_recipients_are_collapsed(client):
    client.put("/api/settings", json={"chat_id": ["111", "111", "222"]})
    assert config.load().chat_ids == ("111", "222")


def test_a_junk_recipient_is_refused_before_anything_is_written(client):
    client.put("/api/settings", json={"chat_id": "111"})
    r = client.put("/api/settings", json={"chat_id": ["222", "not-an-id"]})
    assert r.status_code == 400
    assert "not-an-id" in r.json()["detail"]
    # The good id in the same request must not have been half-applied.
    assert config.load().chat_ids == ("111",)


def test_a_channel_username_is_a_usable_recipient(client):
    r = client.put("/api/settings", json={"chat_id": ["@my_alerts_channel"]})
    assert r.status_code == 200
    assert config.load().chat_ids == ("@my_alerts_channel",)


def test_one_recipient_is_enough_to_be_configured(client):
    client.put("/api/settings", json={"bot_token": TOKEN, "chat_id": ["111", "222"]})
    assert config.load().configured is True
    client.put("/api/settings", json={"chat_id": ""})
    assert config.load().configured is False


def test_changing_the_area_unpins_the_store(client, env):
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "session.json").write_text(
        json.dumps({"cookies": {"aws-waf-token": "x"}, "store_id": "1382", "area_label": "Old"})
    )
    r = client.put("/api/settings", json={"area": "560001"})
    assert r.json()["settings"]["area"] == "560001"
    cached = json.loads((env["data"] / "session.json").read_text())
    assert cached["store_id"] == ""
    # The WAF token itself is still good; only the store pin was dropped.
    assert cached["cookies"]["aws-waf-token"] == "x"


def test_the_settings_file_is_not_world_readable(client, env):
    client.put("/api/settings", json={"bot_token": TOKEN})
    mode = (env["data"] / "settings.json").stat().st_mode & 0o077
    assert mode == 0


def test_timing_is_clamped_by_validation(client):
    assert client.put("/api/settings", json={"poll_minutes": 0}).status_code == 422
    assert client.put("/api/settings", json={"poll_minutes": 99999}).status_code == 422
    assert client.put("/api/settings", json={"poll_minutes": 30}).status_code == 200
    assert config.load().poll_minutes == 30


# ── watches ──────────────────────────────────────────────────────────
def test_watches_round_trip(client, env):
    r = client.put(
        "/api/watches",
        json={
            "watches": [
                {
                    "name": "Milk",
                    "query": "milk",
                    "min_discount_pct": 30,
                    "categories": ["Dairy"],
                    "exclude": ["shake"],
                    "max_price": 80,
                    "enabled": False,
                }
            ]
        },
    )
    assert r.status_code == 200
    on_disk = json.loads(env["watchlist"].read_text())["watches"]
    assert on_disk[0]["query"] == "milk"
    assert on_disk[0]["enabled"] is False
    assert client.get("/api/bootstrap").json()["watches"][0]["max_price"] == 80


def test_a_watch_without_a_query_is_refused(client, env):
    before = env["watchlist"].read_text()
    r = client.put(
        "/api/watches", json={"watches": [{"query": "", "min_discount_pct": 10}]}
    )
    assert r.status_code == 422
    assert env["watchlist"].read_text() == before


def test_an_out_of_range_discount_is_refused(client):
    r = client.put(
        "/api/watches", json={"watches": [{"query": "eggs", "min_discount_pct": 140}]}
    )
    assert r.status_code == 422


# ── actions that should not reach the network ────────────────────────
def test_check_refuses_before_telegram_is_connected(client):
    r = client.post("/api/check", json={"dry_run": False})
    assert r.status_code == 400
    assert "Telegram" in r.json()["detail"]


def test_check_refuses_with_no_enabled_watches(client):
    client.put("/api/settings", json={"bot_token": TOKEN, "chat_id": "1"})
    client.put(
        "/api/watches",
        json={"watches": [{"query": "eggs", "min_discount_pct": 50, "enabled": False}]},
    )
    r = client.post("/api/check", json={"dry_run": True})
    assert r.status_code == 400
    assert "no enabled watches" in r.json()["detail"]


def test_test_alert_refuses_without_credentials(client):
    r = client.post("/api/telegram/test")
    assert r.status_code == 400


# ── maintenance ──────────────────────────────────────────────────────
def test_reset_targets(client, env):
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "session.json").write_text("{}")
    (env["data"] / "alerts.json").write_text("{}")

    assert client.post("/api/reset", json={"target": "session"}).status_code == 200
    assert not (env["data"] / "session.json").exists()
    assert client.post("/api/reset", json={"target": "alerts"}).status_code == 200
    assert not (env["data"] / "alerts.json").exists()
    assert client.post("/api/reset", json={"target": "logs"}).status_code == 200
    assert client.post("/api/reset", json={"target": "nope"}).status_code == 400


def test_poller_switch_persists_and_reports(client):
    client.put(
        "/api/watches",
        json={"watches": [{"query": "eggs", "min_discount_pct": 50, "enabled": False}]},
    )
    r = client.post("/api/poller", json={"enabled": True, "minutes": 45})
    assert r.json()["running"] is True
    assert config.load().poll_enabled is True
    assert config.load().poll_minutes == 45

    r = client.post("/api/poller", json={"enabled": False})
    assert r.json()["running"] is False
    assert config.load().poll_enabled is False


# ── logs ─────────────────────────────────────────────────────────────
def test_saving_a_watchlist_shows_up_in_the_console(client):
    client.put(
        "/api/watches", json={"watches": [{"query": "eggs", "min_discount_pct": 50}]}
    )
    messages = [e["message"] for e in client.get("/api/logs").json()["logs"]]
    assert any("watchlist saved" in m for m in messages)


def test_masking_keeps_the_secret_half_hidden():
    assert webapp.mask("") == ""
    assert webapp.mask(TOKEN) == f"1234567890:{'•' * 6}{TOKEN[-4:]}"
    assert TOKEN[10:-4] not in webapp.mask(TOKEN)


# ── concurrency guard ────────────────────────────────────────────────
def test_a_second_run_cannot_start_while_one_is_claimed():
    """Two clicks a millisecond apart must not both dispatch a pass."""
    from instamart_alerts.scheduler import Scheduler

    sched = Scheduler()
    release = __import__("threading").Event()
    started = __import__("threading").Event()

    def job():
        started.set()
        release.wait(5)

    assert sched.run_in_background("first", job) is True
    started.wait(2)
    assert sched.run_in_background("second", job) is False
    release.set()


# ── behind a reverse proxy ───────────────────────────────────────────
def test_health_needs_no_auth(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app(), client=("203.0.113.9", 5)) as c:
        assert c.get("/api/health").json()["ok"] is True


def test_the_session_cookie_is_marked_secure_behind_https(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        r = c.post(
            "/api/login",
            json={"password": "hunter2"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "secure" in r.headers["set-cookie"].lower()


def test_the_cookie_is_not_secure_over_plain_http(env, monkeypatch):
    """Marking it Secure on a plain-HTTP install would lock the user out."""
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        r = c.post("/api/login", json={"password": "hunter2"})
        assert "secure" not in r.headers["set-cookie"].lower()


def test_a_forwarded_for_header_cannot_fake_a_local_client(env):
    """No password set means loopback-only; a header must not get you in."""
    with TestClient(webapp.create_app(), client=("203.0.113.9", 5)) as c:
        r = c.get("/api/bootstrap", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403


# ── connection settings ──────────────────────────────────────────────
def test_the_build_version_falls_back_to_the_shipped_constant(client):
    assert client.get("/api/bootstrap").json()["settings"]["build_version"] == (
        config.BUILD_VERSION
    )


def test_a_new_build_version_unpins_the_session(client, env):
    """The cached session was minted under the old header, so it has to go."""
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "session.json").write_text("{}")
    r = client.put("/api/settings", json={"build_version": "2.999.0"})
    assert r.json()["settings"]["build_version"] == "2.999.0"
    assert config.load().build_version == "2.999.0"
    assert not (env["data"] / "session.json").exists()


def test_resaving_the_same_build_version_keeps_the_session(client, env):
    env["data"].mkdir(parents=True, exist_ok=True)
    client.put("/api/settings", json={"build_version": "2.999.0"})
    (env["data"] / "session.json").write_text("{}")
    client.put("/api/settings", json={"build_version": "2.999.0"})
    assert (env["data"] / "session.json").exists()


def test_a_junk_build_version_is_refused(client):
    r = client.put("/api/settings", json={"build_version": "2.0.0 OR 1=1"})
    assert r.status_code == 400
    assert config.load().build_version == config.BUILD_VERSION


def test_proxy_round_trips_and_can_be_cleared(client):
    client.put("/api/settings", json={"proxy": "socks5://u:p@host:1080"})
    assert config.load().proxy == "socks5://u:p@host:1080"
    client.put("/api/settings", json={"proxy": ""})
    assert config.load().proxy is None


@pytest.mark.parametrize("bad", ["host:1080", "ftp://host", "socks5://", "javascript:x"])
def test_a_proxy_without_a_usable_scheme_is_refused(client, bad):
    assert client.put("/api/settings", json={"proxy": bad}).status_code == 400


def test_the_environment_block_names_what_the_panel_cannot_change(client):
    env_block = client.get("/api/bootstrap").json()["environment"]
    assert env_block == {
        "password_set": False,
        "headless": True,
        "mini_app_dev_mode": False,
    }


# ── bootstrap diagnostics ────────────────────────────────────────────
def test_no_bootstrap_failure_is_reported_when_none_happened(client):
    assert client.get("/api/bootstrap").json()["bootstrap_failure"] is None
    assert client.get("/api/diagnostics/bootstrap.png").status_code == 404


def test_a_saved_bootstrap_failure_is_offered_to_the_panel(client, env):
    env["data"].mkdir(parents=True, exist_ok=True)
    (env["data"] / "bootstrap-failure.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")

    fail = client.get("/api/bootstrap").json()["bootstrap_failure"]
    assert fail["screenshot"] == "/api/diagnostics/bootstrap.png"
    assert fail["at"] > 0

    r = client.get("/api/diagnostics/bootstrap.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_the_bootstrap_screenshot_is_not_public(env, monkeypatch):
    monkeypatch.setenv("IM_WEB_PASSWORD", "hunter2")
    with TestClient(webapp.create_app()) as c:
        assert c.get("/api/diagnostics/bootstrap.png").status_code == 401


def test_the_bootstrap_wait_is_clamped(client):
    assert client.put("/api/settings", json={"bootstrap_seconds": 2}).status_code == 422
    assert client.put("/api/settings", json={"bootstrap_seconds": 999}).status_code == 422
    assert client.put("/api/settings", json={"bootstrap_seconds": 90}).status_code == 200
    assert config.load().bootstrap_seconds == 90


# ── fetch mode ───────────────────────────────────────────────────────
def test_the_default_fetch_mode_is_auto(client):
    body = client.get("/api/bootstrap").json()["settings"]
    assert body["transport"] == "auto"
    assert body["transports"] == ["auto", "http", "browser"]


@pytest.mark.parametrize("mode", ["auto", "http", "browser"])
def test_each_fetch_mode_round_trips(client, mode):
    assert client.put("/api/settings", json={"transport": mode}).status_code == 200
    assert config.load().transport == mode


def test_an_unknown_fetch_mode_is_refused(client):
    r = client.put("/api/settings", json={"transport": "carrier-pigeon"})
    assert r.status_code == 400
    assert config.load().transport == "auto"


def test_a_junk_mode_in_the_environment_falls_back_to_auto(client, monkeypatch):
    """A typo in IM_TRANSPORT must not stop the poller dead."""
    monkeypatch.setenv("IM_TRANSPORT", "htpp")
    assert config.load().transport == "auto"
