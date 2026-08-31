"""Standalone control panel: a real web app, not a Telegram Mini App.

The Mini App in `server.py` borrows Telegram's signed `initData` for auth, which
means it only opens from inside Telegram. This one stands on its own — you point
a browser at it, and from there you set the bot token and chat id, edit watches,
watch the log console live, and fire a test alert. Because it can *write* the bot
token, it guards itself:

  * `IM_WEB_PASSWORD` set   → a login page and a signed session cookie
  * `IM_WEB_PASSWORD` unset → loopback clients only; anything else gets a 403

Instamart calls block (sync httpx, plus Playwright when a token needs minting),
so they are handed to the scheduler's single worker rather than run inline, and
progress reaches the browser over the same SSE stream as the logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, logbus
from .instamart import ensure_location, search
from .notify import send_to
from .runner import open_session, run_once
from .scheduler import Scheduler
from .session import Blocked, save_cached, sync_cookies
from .watchlist import Watch, Watchlist

log = logging.getLogger("panel")

PANEL = Path(__file__).parent / "static" / "panel"
COOKIE = "im_panel"
SESSION_DAYS = 30
TELEGRAM_TIMEOUT = 20.0

scheduler = Scheduler()


# ── auth ─────────────────────────────────────────────────────────────
def password() -> str:
    return os.getenv("IM_WEB_PASSWORD", "").strip()


def _secret(settings: config.Settings) -> str:
    """A per-install key for signing session cookies; minted on first use."""
    over = config.read_overrides(settings.data_dir)
    key = str(over.get("web_secret") or "")
    if not key:
        key = secrets.token_hex(32)
        config.write_overrides(settings.data_dir, {"web_secret": key})
    return key


def _sign(settings: config.Settings, issued: int) -> str:
    mac = hmac.new(
        _secret(settings).encode(), f"{issued}".encode(), sha256
    ).hexdigest()
    return f"{issued}.{mac}"


def _valid_cookie(settings: config.Settings, raw: str | None) -> bool:
    if not raw or "." not in raw:
        return False
    issued_s, _, _ = raw.partition(".")
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    if time.time() - issued > SESSION_DAYS * 86400:
        return False
    return hmac.compare_digest(_sign(settings, issued), raw)


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient", "")


def authed(request: Request) -> bool:
    settings = config.load()
    if password():
        return _valid_cookie(settings, request.cookies.get(COOKIE))
    return _is_local(request)


def require_auth(request: Request) -> config.Settings:
    if authed(request):
        return config.load()
    if password():
        raise HTTPException(status_code=401, detail="sign in to continue")
    raise HTTPException(
        status_code=403,
        detail=(
            "this panel only answers localhost. Set IM_WEB_PASSWORD to reach it "
            "from another machine."
        ),
    )


Auth = Depends(require_auth)


# ── request bodies ───────────────────────────────────────────────────
class LoginIn(BaseModel):
    password: str = ""


class WatchIn(BaseModel):
    name: str = ""
    query: str = Field(min_length=1)
    min_discount_pct: float = Field(ge=0, le=100)
    categories: list[str] = []
    include: list[str] = []
    exclude: list[str] = []
    max_price: float | None = None
    in_stock_only: bool = True
    enabled: bool = True


class WatchesIn(BaseModel):
    watches: list[WatchIn]


class SettingsIn(BaseModel):
    """Every field optional — the panel saves one card at a time."""

    bot_token: str | None = None
    # Either shape works: "111, 222" from a hand-edited client, or a list from
    # the panel's chip field.
    chat_id: str | list[str] | None = None
    area: str | None = None
    proxy: str | None = None
    poll_minutes: int | None = Field(default=None, ge=1, le=1440)
    cooldown_hours: float | None = Field(default=None, ge=0, le=8760)
    build_version: str | None = Field(default=None, max_length=32)


class PollerIn(BaseModel):
    enabled: bool
    minutes: int | None = Field(default=None, ge=1, le=1440)


class ResetIn(BaseModel):
    target: str


# ── helpers ──────────────────────────────────────────────────────────
# A chat id is a signed integer, or an @username for a public channel.
CHAT_ID_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
PROXY_RE = re.compile(r"^(socks5h?|https?)://\S+$")
BUILD_RE = re.compile(r"^[\w.\-]{1,32}$")


def mask(token: str) -> str:
    """Enough of the token to recognise it, not enough to use it."""
    if not token:
        return ""
    head, _, tail = token.partition(":")
    return f"{head}:{'•' * 6}{tail[-4:]}" if tail else "•" * 8


def _watchlist(settings: config.Settings) -> Watchlist:
    return Watchlist.load(settings.watchlist_path)


def _pin_store_again(settings: config.Settings) -> None:
    """Changing the area means a different dark store, so drop the pinned one."""
    cache = settings.data_dir / "session.json"
    if not cache.exists():
        return
    try:
        data = json.loads(cache.read_text())
        data["store_id"] = ""
        data["area_label"] = ""
        cache.write_text(json.dumps(data, indent=2))
    except (OSError, ValueError):
        cache.unlink(missing_ok=True)


def _store_info(settings: config.Settings) -> dict[str, Any]:
    cache = settings.data_dir / "session.json"
    if not cache.exists():
        return {"store_id": "", "area_label": "", "minted_at": 0}
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        return {"store_id": "", "area_label": "", "minted_at": 0}
    return {
        "store_id": str(data.get("store_id") or ""),
        "area_label": str(data.get("area_label") or ""),
        "minted_at": data.get("minted_at") or 0,
    }


def _telegram(settings: config.Settings, method: str, **params) -> dict[str, Any]:
    if not settings.bot_token:
        raise HTTPException(status_code=400, detail="no bot token saved yet")
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{settings.bot_token}/{method}",
            params=params,
            timeout=TELEGRAM_TIMEOUT,
            proxy=settings.proxy,
        )
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"telegram unreachable: {e}") from None


def _snapshot(settings: config.Settings) -> dict[str, Any]:
    wl = _watchlist(settings)
    return {
        "settings": {
            "bot_token_masked": mask(settings.bot_token),
            "bot_token_set": bool(settings.bot_token),
            "chat_id": settings.chat_id,
            "chat_ids": list(settings.chat_ids),
            "area": settings.area,
            "proxy": settings.proxy or "",
            "poll_minutes": settings.poll_minutes,
            "cooldown_hours": settings.cooldown_hours,
            "build_version": settings.build_version,
            "build_version_default": config.BUILD_VERSION,
            "telegram_ready": settings.configured,
        },
        # Set outside the panel, shown so it is obvious what it cannot change.
        "environment": {
            "password_set": bool(password()),
            "headless": settings.headless,
            "mini_app_dev_mode": settings.dev_mode,
        },
        "watches": [w.to_dict() for w in wl.watches],
        "store": _store_info(settings),
        "scheduler": scheduler.status(),
        "watchlist_path": str(settings.watchlist_path),
        "data_dir": str(settings.data_dir),
        "auth": {"password_required": bool(password())},
    }


# ── app ──────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    settings = config.load()
    logbus.install(settings.data_dir)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        current = config.load()
        if current.poll_enabled:
            log.info("resuming poller from saved settings")
            scheduler.start(current.poll_minutes)
        yield
        scheduler.stop()

    app = FastAPI(
        title="instamart control panel",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ── shell ────────────────────────────────────────────────────────
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(PANEL / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        """Unauthenticated, for container health checks and uptime pings."""
        return {"ok": True, "poller": scheduler.running}

    @app.get("/api/session")
    def session_state(request: Request) -> dict:
        return {
            "authed": authed(request),
            "password_required": bool(password()),
            "local": _is_local(request),
        }

    @app.post("/api/login")
    def login(request: Request, response: Response, body: LoginIn) -> dict:
        want = password()
        if not want:
            raise HTTPException(status_code=400, detail="no password is configured")
        if not hmac.compare_digest(body.password, want):
            log.warning("failed panel login from %s", request.client.host if request.client else "?")
            raise HTTPException(status_code=401, detail="wrong password")
        # Behind a TLS-terminating proxy the connection we see is plain HTTP, so
        # take the scheme from the forwarded header. Client IP is deliberately
        # NOT read from headers — `_is_local` must stay unspoofable.
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        response.set_cookie(
            COOKIE,
            _sign(config.load(), int(time.time())),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=(forwarded or request.url.scheme) == "https",
        )
        log.info("panel unlocked")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ── state ────────────────────────────────────────────────────────
    @app.get("/api/bootstrap")
    def bootstrap(settings: config.Settings = Auth) -> dict:
        return _snapshot(settings) | {
            "logs": logbus.history(600),
            "runs": logbus.runs(),
            "now": time.time(),
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsIn, settings: config.Settings = Auth) -> dict:
        changes: dict[str, Any] = {}
        touched: list[str] = []

        if body.bot_token is not None:
            token = body.bot_token.strip()
            # An unchanged field posts back the mask; do not save that over the
            # real token.
            if "•" not in token:
                changes["bot_token"] = token
                touched.append("bot token")
        if body.chat_id is not None:
            ids = config.parse_chat_ids(body.chat_id)
            bad = [i for i in ids if not CHAT_ID_RE.match(i)]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail=f"not a usable chat id: {', '.join(bad)}",
                )
            changes["chat_id"] = ", ".join(ids)
            touched.append(f"{len(ids)} recipient{'' if len(ids) == 1 else 's'}")
        if body.proxy is not None:
            proxy = body.proxy.strip()
            if proxy and not PROXY_RE.match(proxy):
                raise HTTPException(
                    status_code=400,
                    detail="proxy must look like socks5://… http:// or https://",
                )
            changes["proxy"] = proxy
            touched.append("proxy" if proxy else "proxy (cleared)")
        if body.build_version is not None:
            build = body.build_version.strip()
            if build and not BUILD_RE.match(build):
                raise HTTPException(
                    status_code=400, detail="build version looks like 2.367.0"
                )
            changes["build_version"] = build
            touched.append("build version")
            # The pinned session was minted under the old header; drop it so the
            # next call goes out with the new one.
            if build != settings.build_version:
                (settings.data_dir / "session.json").unlink(missing_ok=True)
        if body.poll_minutes is not None:
            changes["poll_minutes"] = int(body.poll_minutes)
            touched.append("interval")
        if body.cooldown_hours is not None:
            changes["cooldown_hours"] = float(body.cooldown_hours)
            touched.append("cooldown")
        if body.area is not None:
            area = body.area.strip()
            if area and area != settings.area:
                changes["area"] = area
                touched.append("area")
                _pin_store_again(settings)
                log.info("area set to %r — the dark store will be re-resolved", area)
            elif area:
                changes["area"] = area

        if changes:
            config.write_overrides(settings.data_dir, changes)
        if touched:
            log.info("saved: %s", ", ".join(touched))

        fresh = config.load()
        if body.poll_minutes is not None and scheduler.running:
            scheduler.start(fresh.poll_minutes)
        return _snapshot(fresh)

    @app.put("/api/watches")
    def put_watches(body: WatchesIn, settings: config.Settings = Auth) -> dict:
        try:
            watches = [Watch.from_dict(w.model_dump()) for w in body.watches]
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"bad watch: {e}") from None
        Watchlist(watches).save(settings.watchlist_path)
        enabled = sum(1 for w in watches if w.enabled)
        log.info("watchlist saved — %d watch(es), %d enabled", len(watches), enabled)
        return {"watches": [w.to_dict() for w in watches]}

    # ── telegram ─────────────────────────────────────────────────────
    @app.get("/api/telegram/identity")
    def telegram_identity(settings: config.Settings = Auth) -> dict:
        me = _telegram(settings, "getMe")
        if not me.get("ok"):
            return {"ok": False, "error": me.get("description", "token rejected")}
        r = me["result"]
        return {
            "ok": True,
            "username": r.get("username", ""),
            "name": r.get("first_name", ""),
            "id": r.get("id"),
        }

    @app.get("/api/telegram/chats")
    def telegram_chats(settings: config.Settings = Auth) -> dict:
        """Chats that have messaged the bot — a bot cannot open one first."""
        me = _telegram(settings, "getMe")
        if not me.get("ok"):
            return {"ok": False, "error": me.get("description", "token rejected"), "chats": []}
        updates = _telegram(settings, "getUpdates", limit=100)
        seen: dict[str, dict[str, Any]] = {}
        for u in updates.get("result") or []:
            msg = u.get("message") or u.get("my_chat_member") or u.get("edited_message") or {}
            chat = msg.get("chat")
            if not chat:
                continue
            seen[str(chat["id"])] = {
                "id": str(chat["id"]),
                "type": chat.get("type", ""),
                "label": chat.get("username")
                or chat.get("first_name")
                or chat.get("title")
                or "",
                "added": str(chat["id"]) in settings.chat_ids,
            }
        return {
            "ok": True,
            "username": me["result"].get("username", ""),
            "chats": list(seen.values()),
        }

    @app.post("/api/telegram/test")
    def telegram_test(settings: config.Settings = Auth) -> dict:
        if not settings.configured:
            raise HTTPException(
                status_code=400, detail="save a bot token and a chat id first"
            )
        text = (
            "✅ <b>Instamart alerts</b> — test alert\n\n"
            "Delivery is wired up correctly.\n"
            f"<i>{settings.area or 'no area set'}</i>"
        )
        # Sent one at a time so the panel can name exactly who did not get it.
        delivered = [c for c in settings.chat_ids if send_to(settings, c, text)]
        failed = [c for c in settings.chat_ids if c not in delivered]

        if delivered:
            log.info("test alert delivered to %s", ", ".join(delivered))
        if failed:
            log.error(
                "test alert not delivered to %s — see Telegram's reason above",
                ", ".join(failed),
            )
        return {
            "ok": not failed,
            "delivered": delivered,
            "failed": failed,
            "error": (
                ""
                if not failed
                else (
                    f"Telegram refused {', '.join(failed)}. Check the id, and make "
                    "sure that account has pressed Start on the bot."
                )
            ),
        }

    # ── instamart ────────────────────────────────────────────────────
    @app.get("/api/preview")
    def preview(
        query: str = Query(min_length=1), settings: config.Settings = Auth
    ) -> dict:
        """Live search results, so thresholds can be set against real numbers."""
        if not settings.area:
            raise HTTPException(status_code=400, detail="set your delivery area first")

        with scheduler.lock:
            log.info("preview: searching %r", query)
            client, data = open_session(settings)
            try:
                try:
                    ensure_location(client, data, settings.area)
                    products = search(client, data.store_id, query)
                except Blocked:
                    client.close()
                    client, data = open_session(
                        settings, force_refresh=True, previous=data
                    )
                    ensure_location(client, data, settings.area)
                    products = search(client, data.store_id, query)
                if sync_cookies(client, data):
                    save_cached(settings, data)
            except Exception as e:  # noqa: BLE001 — surfaced to the UI
                log.exception("preview failed")
                raise HTTPException(status_code=502, detail=str(e)) from None
            finally:
                client.close()

        log.info("preview: %d variants from store %s", len(products), data.store_id)
        return {
            "store_id": data.store_id,
            "area": data.area_label,
            "query": query,
            "products": [
                asdict(p) | {"url": p.url}
                for p in sorted(products, key=lambda x: -x.discount_pct)
            ],
        }

    @app.post("/api/check")
    def check(
        dry_run: bool = Body(default=False, embed=True),
        settings: config.Settings = Auth,
    ) -> dict:
        """Kick off one pass. The result arrives on the event stream."""
        if not settings.area:
            raise HTTPException(status_code=400, detail="set your delivery area first")
        wl = _watchlist(settings)
        if not wl.active:
            raise HTTPException(status_code=400, detail="no enabled watches to run")
        if not dry_run and not settings.configured:
            raise HTTPException(
                status_code=400, detail="connect Telegram first, or run a dry run"
            )

        label = "manual dry run" if dry_run else "manual check"
        started = scheduler.run_in_background(
            label,
            lambda: scheduler.execute(
                label,
                lambda: run_once(
                    settings,
                    wl,
                    dry_run=dry_run,
                    cooldown_hours=settings.cooldown_hours,
                ),
                trigger="manual",
                dry_run=dry_run,
            ),
        )
        if not started:
            raise HTTPException(status_code=409, detail="a run is already in progress")
        return {"started": True, "dry_run": dry_run}

    @app.post("/api/poller")
    def poller(body: PollerIn, settings: config.Settings = Auth) -> dict:
        minutes = body.minutes or settings.poll_minutes
        config.write_overrides(
            settings.data_dir,
            {"poll_enabled": body.enabled, "poll_minutes": int(minutes)},
        )
        if body.enabled:
            scheduler.start(int(minutes))
        else:
            scheduler.stop()
        return scheduler.status()

    @app.post("/api/reset")
    def reset(body: ResetIn, settings: config.Settings = Auth) -> dict:
        target = body.target
        if target == "session":
            (settings.data_dir / "session.json").unlink(missing_ok=True)
            log.info("session cleared — the next call will re-mint a WAF token")
        elif target == "alerts":
            (settings.data_dir / "alerts.json").unlink(missing_ok=True)
            log.info("alert history cleared — every live deal can fire again")
        elif target == "logs":
            logbus.clear()
        else:
            raise HTTPException(status_code=400, detail=f"unknown target {target!r}")
        return {"ok": True, "target": target}

    # ── event stream ─────────────────────────────────────────────────
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        if not authed(request):
            raise HTTPException(status_code=401, detail="sign in to continue")

        async def stream():
            q = logbus.subscribe()
            try:
                yield _sse({"type": "hello", "scheduler": scheduler.status()})
                idle = 0.0
                while True:
                    if await request.is_disconnected():
                        return
                    drained = False
                    while True:
                        try:
                            yield _sse(q.get_nowait())
                        except queue.Empty:
                            break
                        drained = True
                    idle = 0.0 if drained else idle + 0.25
                    if idle >= 15:
                        idle = 0.0
                        yield ": keepalive\n\n"  # keeps proxies from closing us
                    await asyncio.sleep(0.25)
            finally:
                logbus.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/logs")
    def logs(limit: int = Query(default=600, ge=1, le=1500), _: config.Settings = Auth) -> dict:
        return {"logs": logbus.history(limit), "runs": logbus.runs()}

    app.mount("/panel", StaticFiles(directory=PANEL), name="panel")
    return app


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
