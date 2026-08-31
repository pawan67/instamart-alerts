"""Telegram Mini App `initData` verification.

The Mini App hands the page a signed `initData` query string. Telegram's scheme:

    secret  = HMAC_SHA256(key="WebAppData", msg=<bot token>)
    check   = "\\n".join(f"{k}={v}" for k, v in sorted(fields) if k != "hash")
    valid   = HMAC_SHA256(key=secret, msg=check).hexdigest() == fields["hash"]

Anyone can POST to this server, so every mutating call is verified this way and
then narrowed to the Telegram users listed in TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass

from .config import Settings

log = logging.getLogger(__name__)

# Telegram recommends rejecting stale initData; the app refreshes it on open.
MAX_AGE_SECONDS = 24 * 3600


class AuthError(Exception):
    """initData was missing, malformed, unsigned, stale, or from another user."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    username: str
    first_name: str


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify(init_data: str, settings: Settings) -> TelegramUser:
    """Validate a raw initData string and return the user it belongs to."""
    if not init_data:
        raise AuthError("missing initData")
    if not settings.bot_token:
        raise AuthError("server has no bot token configured")

    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    if not received:
        raise AuthError("initData has no hash")

    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    expected = hmac.new(
        _secret_key(settings.bot_token), check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("bad initData signature")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise AuthError("bad auth_date") from None
    if auth_date <= 0 or time.time() - auth_date > MAX_AGE_SECONDS:
        raise AuthError("initData has expired — reopen the app")

    try:
        user = json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError:
        raise AuthError("bad user payload") from None
    uid = user.get("id")
    if uid is None:
        raise AuthError("initData has no user")

    # A valid signature only proves the request came through Telegram, not that
    # it came from the owner. Pin it to the configured recipients.
    allowed = settings.chat_ids
    if allowed and str(uid) not in allowed:
        raise AuthError("this bot is not configured for your account")

    return TelegramUser(
        id=int(uid),
        username=user.get("username", ""),
        first_name=user.get("first_name", ""),
    )
