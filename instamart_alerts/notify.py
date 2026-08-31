"""Telegram delivery."""

from __future__ import annotations

import html
import logging

import httpx

from .config import Settings
from .instamart import Product

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


def _escape(s: str) -> str:
    return html.escape(s, quote=False)


def format_alert(watch_name: str, area: str, hits: list[Product]) -> str:
    lines = [
        f"🥚 <b>{_escape(watch_name)}</b> — {len(hits)} deal"
        f"{'s' if len(hits) != 1 else ''} over threshold",
        f"<i>{_escape(area)}</i>",
        "",
    ]
    for p in sorted(hits, key=lambda x: -x.discount_pct):
        lines.append(
            f"<b>{p.discount_pct:g}% off</b> — ₹{p.price:g} "
            f"<s>₹{p.mrp:g}</s>\n"
            f'<a href="{p.url}">{_escape(p.name)}</a> · {_escape(p.quantity)}'
            + (f" · {_escape(p.unit_price)}" if p.unit_price else "")
        )
        lines.append("")
    return "\n".join(lines).strip()


def send_to(settings: Settings, chat_id: str, text: str) -> bool:
    """Post one message to one chat. Returns True on success."""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20.0,
            proxy=settings.proxy,
        )
    except httpx.HTTPError as e:
        log.error("telegram request failed for chat %s: %s", chat_id, e)
        return False

    if r.status_code != 200:
        log.error(
            "telegram rejected the message for chat %s: %s %s",
            chat_id,
            r.status_code,
            r.text[:300],
        )
        return False
    return True


def send(settings: Settings, text: str) -> bool:
    """Post to every configured chat.

    True when at least one recipient got it. Anything stricter would make one
    bad id — a typo, someone who never pressed Start — roll back the whole
    alert and re-send it to the healthy chats on every pass afterwards.
    """
    if not settings.configured:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — cannot send")
        return False

    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…"

    recipients = settings.chat_ids
    delivered = [cid for cid in recipients if send_to(settings, cid, text)]

    if not delivered:
        return False
    if len(delivered) < len(recipients):
        missed = [c for c in recipients if c not in delivered]
        log.warning(
            "delivered to %d of %d chats; no luck with %s",
            len(delivered),
            len(recipients),
            ", ".join(missed),
        )
    elif len(recipients) > 1:
        log.info("delivered to %d chats", len(delivered))
    return True
