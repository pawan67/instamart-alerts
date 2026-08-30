"""Telegram Mini App backend.

Every endpoint takes the Mini App's signed `initData` in an `X-Init-Data`
header, verifies it, and narrows to the configured chat id. Instamart calls are
blocking (sync httpx, and Playwright when a token needs minting), so the routes
are plain `def` and FastAPI runs them in a worker thread. A lock keeps
concurrent Instamart work to one at a time — the WAF does not reward parallelism.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .instamart import ensure_location, search
from .runner import open_session, run_once
from .session import Blocked, save_cached, sync_cookies
from .watchlist import Watch, Watchlist
from .webauth import AuthError, verify

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# Instamart work is serialised; the WAF is happier and a single browser
# bootstrap must never run twice at once.
_instamart_lock = threading.Lock()


def _auth(init_data: str | None) -> config.Settings:
    settings = config.load()
    if settings.dev_mode:
        return settings
    try:
        verify(init_data or "", settings)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from None
    return settings


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
    area: str = Field(min_length=1)


def _load_watchlist(settings: config.Settings) -> Watchlist:
    if not settings.watchlist_path.exists():
        return Watchlist([])
    return Watchlist.load(settings.watchlist_path)


def create_app() -> FastAPI:
    app = FastAPI(title="instamart-alerts", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def get_state(x_init_data: str | None = Header(default=None)) -> dict:
        settings = _auth(x_init_data)
        wl = _load_watchlist(settings)
        return {
            "area": settings.area,
            "watches": [w.to_dict() for w in wl.watches],
            "telegram_ready": settings.configured,
            "dev_mode": settings.dev_mode,
        }

    @app.put("/api/watches")
    def put_watches(
        body: WatchesIn, x_init_data: str | None = Header(default=None)
    ) -> dict:
        settings = _auth(x_init_data)
        try:
            watches = [Watch.from_dict(w.model_dump()) for w in body.watches]
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"bad watch: {e}") from None
        Watchlist(watches).save(settings.watchlist_path)
        return {"saved": len(watches)}

    @app.put("/api/settings")
    def put_settings(
        body: SettingsIn, x_init_data: str | None = Header(default=None)
    ) -> dict:
        settings = _auth(x_init_data)
        area = body.area.strip()
        # Changing the area means a different dark store; drop the pinned one so
        # the next call re-resolves rather than reporting another store's prices.
        config.write_overrides(settings.data_dir, {"area": area})
        cache = settings.data_dir / "session.json"
        if cache.exists():
            import json as _json

            try:
                d = _json.loads(cache.read_text())
                d["store_id"] = ""
                d["area_label"] = ""
                cache.write_text(_json.dumps(d, indent=2))
            except (OSError, ValueError):
                cache.unlink(missing_ok=True)
        return {"area": area}

    @app.get("/api/preview")
    def preview(
        query: str = Query(min_length=1),
        x_init_data: str | None = Header(default=None),
    ) -> dict:
        """Live search results, so thresholds can be set against real numbers."""
        settings = _auth(x_init_data)
        if not settings.area:
            raise HTTPException(status_code=400, detail="set your area first")

        with _instamart_lock:
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

        return {
            "store_id": data.store_id,
            "area": data.area_label,
            "products": [
                asdict(p) | {"url": p.url}
                for p in sorted(products, key=lambda x: -x.discount_pct)
            ],
        }

    @app.post("/api/check")
    def check(
        dry_run: bool = Body(default=False, embed=True),
        x_init_data: str | None = Header(default=None),
    ) -> dict:
        settings = _auth(x_init_data)
        if not settings.area:
            raise HTTPException(status_code=400, detail="set your area first")
        wl = _load_watchlist(settings)
        if not wl.active:
            raise HTTPException(status_code=400, detail="no enabled watches")

        with _instamart_lock:
            try:
                results = run_once(settings, wl, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — surfaced to the UI
                log.exception("check failed")
                raise HTTPException(status_code=502, detail=str(e)) from None

        return {
            "dry_run": dry_run,
            "results": [
                {
                    "name": r.watch.name,
                    "threshold": r.watch.min_discount_pct,
                    "tracked": len(r.candidates),
                    "best": max((p.discount_pct for p in r.candidates), default=0.0),
                    "hits": len(r.hits),
                    "error": r.error,
                    "alerted": [
                        {
                            "name": p.name,
                            "quantity": p.quantity,
                            "price": p.price,
                            "mrp": p.mrp,
                            "discount_pct": p.discount_pct,
                            "url": p.url,
                        }
                        for p in sorted(r.alerted, key=lambda x: -x.discount_pct)
                    ],
                }
                for r in results
            ],
        }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
