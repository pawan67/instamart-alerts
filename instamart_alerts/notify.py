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


def send(settings: Settings, text: str) -> bool:
    """Post to Telegram. Returns True on success."""
    if not settings.configured:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — cannot send")
        return False

    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…"

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{settings.bot_token}/sendMessage",
            json={
                "chat_id": settings.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20.0,
            proxy=settings.proxy,
        )
    except httpx.HTTPError as e:
        log.error("telegram request failed: %s", e)
        return False

    if r.status_code != 200:
        log.error("telegram rejected the message: %s %s", r.status_code, r.text[:300])
        return False
    return True
