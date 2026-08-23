"""Mini App initData verification — the only thing standing between the
public internet and the settings, so it gets exercised properly."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import pytest

from instamart_alerts.config import Settings
from instamart_alerts.webauth import AuthError, verify

TOKEN = "8769371768:AAGtesttokenvaluenotarealbotsecret00"
UID = 958113963


def settings(tmp_path: Path, *, chat_id: str = str(UID), token: str = TOKEN) -> Settings:
    return Settings(
        bot_token=token,
        chat_id=chat_id,
        area="401209",
        proxy=None,
        data_dir=tmp_path,
        watchlist_path=tmp_path / "watchlist.json",
        headless=True,
    )


def sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Build a correctly signed initData string, the way Telegram does."""
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def init_data(**over) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF",
        "user": json.dumps({"id": UID, "username": "tester", "first_name": "T"}),
    }
    fields.update({k: v for k, v in over.items() if v is not None})
    return sign(fields, over.pop("_token", TOKEN))


def test_valid_init_data_passes(tmp_path):
    user = verify(init_data(), settings(tmp_path))
    assert user.id == UID
    assert user.username == "tester"


def test_empty_is_rejected(tmp_path):
    with pytest.raises(AuthError):
        verify("", settings(tmp_path))


def test_missing_hash_is_rejected(tmp_path):
    raw = urllib.parse.urlencode({"auth_date": str(int(time.time())), "user": "{}"})
    with pytest.raises(AuthError, match="no hash"):
        verify(raw, settings(tmp_path))


def test_tampered_field_is_rejected(tmp_path):
    # Take valid data, then swap the user for someone else, keeping the hash.
    raw = init_data()
    fields = dict(urllib.parse.parse_qsl(raw))
    fields["user"] = json.dumps({"id": 1, "username": "attacker"})
    with pytest.raises(AuthError, match="signature"):
        verify(urllib.parse.urlencode(fields), settings(tmp_path))


def test_signature_from_a_different_token_is_rejected(tmp_path):
    other = "1111111111:BBBdifferenttokenvalue0000000000000"
    with pytest.raises(AuthError, match="signature"):
        verify(init_data(_token=other), settings(tmp_path))


def test_expired_init_data_is_rejected(tmp_path):
    old = str(int(time.time()) - 48 * 3600)
    with pytest.raises(AuthError, match="expired"):
        verify(init_data(auth_date=old), settings(tmp_path))


def test_another_telegram_user_is_rejected(tmp_path):
    # Correctly signed by the real bot, but not the owner's account.
    raw = init_data(user=json.dumps({"id": 42, "username": "someone"}))
    with pytest.raises(AuthError, match="not configured"):
        verify(raw, settings(tmp_path))


def test_server_without_token_rejects(tmp_path):
    with pytest.raises(AuthError, match="bot token"):
        verify(init_data(), settings(tmp_path, token=""))


def test_missing_user_is_rejected(tmp_path):
    raw = sign({"auth_date": str(int(time.time())), "query_id": "AAF"})
    with pytest.raises(AuthError, match="no user"):
        verify(raw, settings(tmp_path))


def test_zero_auth_date_is_rejected(tmp_path):
    with pytest.raises(AuthError, match="expired"):
        verify(init_data(auth_date="0"), settings(tmp_path))
