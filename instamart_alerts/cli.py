"""Command line entry point.

    uv run im check            # one pass, sends Telegram alerts
    uv run im check --dry-run  # same, but prints instead of sending
    uv run im list eggs        # show everything the search returns right now
    uv run im watch --every 15 # poll forever
    uv run im test-telegram    # verify bot token / chat id
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import config
from .notify import send
from .runner import open_session, run_once
from .instamart import ensure_location, search
from .session import Blocked, save_cached, sync_cookies
from .watchlist import Watchlist


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _require_area(settings: config.Settings) -> None:
    if not settings.area:
        sys.exit("IM_AREA is not set — put your area or pincode in .env")


def cmd_check(args: argparse.Namespace) -> int:
    settings = config.load()
    _require_area(settings)
    if not args.dry_run and not settings.configured:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — set them in .env")

    watchlist = Watchlist.load(settings.watchlist_path)
    if not watchlist.active:
        sys.exit(f"no enabled watches in {settings.watchlist_path}")

    results = run_once(
        settings, watchlist, dry_run=args.dry_run, cooldown_hours=args.cooldown
    )

    for r in results:
        if r.error:
            print(f"✗ {r.watch.name}: {r.error}")
            continue
        best = max((p.discount_pct for p in r.candidates), default=0.0)
        print(
            f"• {r.watch.name}: {len(r.candidates)} tracked, best {best:g}% "
            f"(threshold {r.watch.min_discount_pct:g}%), "
            f"{len(r.hits)} over, {len(r.alerted)} alerted"
        )
        for p in sorted(r.alerted, key=lambda x: -x.discount_pct):
            prefix = "  would alert:" if args.dry_run else "  alerted:"
            print(f"{prefix} {p}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = config.load()
    _require_area(settings)
    client, data = open_session(settings)
    try:
        try:
            ensure_location(client, data, settings.area)
            products = search(client, data.store_id, args.query)
        except Blocked:
            client.close()
            client, data = open_session(settings, force_refresh=True, previous=data)
            ensure_location(client, data, settings.area)
            products = search(client, data.store_id, args.query)
        if sync_cookies(client, data):
            save_cached(settings, data)
    finally:
        client.close()

    print(f"\n{data.area_label} · store {data.store_id} · query {args.query!r}\n")
    print(f"{'disc%':>6} {'price':>8} {'mrp':>8}  {'category':<16} name")
    for p in sorted(products, key=lambda x: -x.discount_pct):
        flag = "" if p.in_stock else "  (out of stock)"
        print(
            f"{p.discount_pct:>6g} {p.price:>8g} {p.mrp:>8g}  "
            f"{p.category[:16]:<16} {p.name} [{p.quantity}]{flag}"
        )
    print(f"\n{len(products)} variants")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    settings = config.load()
    _require_area(settings)
    if not settings.configured:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — set them in .env")
    watchlist = Watchlist.load(settings.watchlist_path)
    log = logging.getLogger("watch")
    log.info("polling every %d min; Ctrl-C to stop", args.every)
    while True:
        try:
            run_once(settings, watchlist, cooldown_hours=args.cooldown)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 — a long-running poller must not die
            log.exception("poll failed: %s", e)
        try:
            time.sleep(args.every * 60)
        except KeyboardInterrupt:
            return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    settings = config.load()
    if settings.dev_mode:
        logging.getLogger("serve").warning(
            "IM_WEB_DEV=1 — signature checks are OFF; bind to localhost only"
        )
    elif not settings.bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN missing — the web app verifies against it")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_bot(args: argparse.Namespace) -> int:
    from . import bot

    settings = config.load()
    if not settings.configured:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — set them in .env")
    url = args.url or os.getenv("IM_WEBAPP_URL", "")
    if not url:
        sys.exit("pass --url https://… (or set IM_WEBAPP_URL) — your public tunnel")
    try:
        bot.run(settings, url)
    except KeyboardInterrupt:
        return 0
    except ValueError as e:
        sys.exit(str(e))
    return 0


def cmd_chat_id(_: argparse.Namespace) -> int:
    """Resolve the chat id by reading whatever has been sent to the bot."""
    import httpx

    settings = config.load()
    if not settings.bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN missing — set it in .env")

    base = f"https://api.telegram.org/bot{settings.bot_token}"
    me = httpx.get(f"{base}/getMe", timeout=20.0).json()
    if not me.get("ok"):
        sys.exit(f"bad token: {me.get('description')}")
    username = me["result"]["username"]
    print(f"bot: @{username}")

    updates = httpx.get(f"{base}/getUpdates", timeout=20.0).json().get("result") or []
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        print(
            f"\nNo messages yet. Telegram bots cannot open a chat first, so:\n"
            f"  1. open https://t.me/{username}\n"
            f"  2. press Start (or send any message)\n"
            f"  3. run this again"
        )
        return 1

    print("\nchats that have messaged this bot:")
    for cid, chat in chats.items():
        label = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
        print(f"  TELEGRAM_CHAT_ID={cid}   ({chat.get('type')}) {label}")
    return 0


def cmd_test_telegram(_: argparse.Namespace) -> int:
    settings = config.load()
    if not settings.configured:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — set them in .env")
    ok = send(settings, "✅ <b>instamart-alerts</b> is wired up correctly.")
    print("sent" if ok else "failed — see the error above")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="im", description="Swiggy Instamart price alerts")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run one pass over the watchlist")
    c.add_argument("--dry-run", action="store_true", help="print instead of sending")
    c.add_argument("--cooldown", type=float, default=24.0, help="re-alert after N hours")
    c.set_defaults(func=cmd_check)

    lst = sub.add_parser("list", help="dump live search results with discounts")
    lst.add_argument("query", nargs="?", default="eggs")
    lst.set_defaults(func=cmd_list)

    w = sub.add_parser("watch", help="poll on an interval")
    w.add_argument("--every", type=int, default=15, help="minutes between passes")
    w.add_argument("--cooldown", type=float, default=24.0)
    w.set_defaults(func=cmd_watch)

    s = sub.add_parser("serve", help="run the Mini App web server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_serve)

    b = sub.add_parser("bot", help="reply to /start with a Mini App button")
    b.add_argument("--url", default="", help="public HTTPS url of the web app")
    b.set_defaults(func=cmd_bot)

    cid = sub.add_parser("chat-id", help="find the chat id for your bot")
    cid.set_defaults(func=cmd_chat_id)

    t = sub.add_parser("test-telegram", help="send a test message")
    t.set_defaults(func=cmd_test_telegram)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
