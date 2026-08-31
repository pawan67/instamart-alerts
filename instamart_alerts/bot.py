"""A minimal long-polling bot whose only job is to hand you the Mini App button.

Long polling avoids needing a public webhook — the tunnel only has to serve the
web app itself. Telegram requires the Mini App URL to be HTTPS.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

WELCOME = (
    "Instamart price alerts.\n\n"
    "Open the panel to set your area, manage watches, and check prices now."
)


def _api(settings: Settings, method: str, **payload) -> dict:
    r = httpx.post(
        f"https://api.telegram.org/bot{settings.bot_token}/{method}",
        json=payload,
        timeout=70.0,
        proxy=settings.proxy,
    )
    return r.json()


def set_menu_button(settings: Settings, url: str) -> bool:
    """Put a persistent 'Alerts' button next to the chat input, for each recipient."""
    results = []
    for chat_id in settings.chat_ids:
        try:
            res = _api(
                settings,
                "setChatMenuButton",
                chat_id=int(chat_id),
                menu_button={
                    "type": "web_app",
                    "text": "Alerts",
                    "web_app": {"url": url},
                },
            )
        except ValueError:
            log.error("skipping non-numeric chat id %r", chat_id)
            continue
        if not res.get("ok"):
            log.error(
                "setChatMenuButton failed for %s: %s", chat_id, res.get("description")
            )
        results.append(bool(res.get("ok")))
    return any(results)


def run(settings: Settings, url: str) -> None:
    """Poll for /start and reply with a button that opens the Mini App."""
    if not url.startswith("https://"):
        raise ValueError(f"Telegram requires an HTTPS Mini App URL, got {url!r}")

    set_menu_button(settings, url)
    log.info("bot polling; Mini App at %s", url)

    offset = 0
    while True:
        try:
            res = httpx.post(
                f"https://api.telegram.org/bot{settings.bot_token}/getUpdates",
                json={"offset": offset, "timeout": 50, "allowed_updates": ["message"]},
                timeout=70.0,
                proxy=settings.proxy,
            ).json()
        except httpx.HTTPError as e:
            log.warning("getUpdates failed, retrying: %s", e)
            continue

        for update in res.get("result") or []:
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                continue
            allowed = settings.chat_ids
            if allowed and str(chat_id) not in allowed:
                log.info("ignoring message from chat %s", chat_id)
                continue

            _api(
                settings,
                "sendMessage",
                chat_id=chat_id,
                text=WELCOME,
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "⚙️ Open panel", "web_app": {"url": url}}]
                    ]
                },
            )
