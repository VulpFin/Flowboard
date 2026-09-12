# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Application factory / ASGI entry point (`uvicorn app.main:app`)."""
from __future__ import annotations

import logging
import os
from urllib.parse import quote

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import settings
from .web import deps
from .web.routers import ai as ai_router
from .web.routers import auth as auth_router
from .web.routers import boards as boards_router
from .web.routers import calendars as calendars_router
from .web.routers import settings as settings_router
from .web.routers import tasks as tasks_router

log = logging.getLogger("flowboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Make sure the schema exists in dev/test (alembic is the source of truth in prod).
    if settings.is_dev:
        from .db import Base, engine
        from . import models  # noqa: F401

        Base.metadata.create_all(engine)
    log.info("Flowboard %s starting (env=%s, site=%s)", __version__, settings.FLOWBOARD_ENV, settings.FLOWBOARD_SITE_URL)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.FLOWBOARD_SITE_NAME, version=__version__, lifespan=_lifespan, docs_url="/api/docs" if settings.is_dev else None, redoc_url=None, openapi_url="/api/openapi.json" if settings.is_dev else None)

    if settings.FLOWBOARD_PROXY_HEADERS:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    app.add_middleware(SecurityHeadersMiddleware)
    if not settings.is_dev:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    app.include_router(auth_router.router)
    app.include_router(boards_router.router)
    app.include_router(tasks_router.router)
    app.include_router(ai_router.router)
    app.include_router(settings_router.router)
    app.include_router(calendars_router.router)

    @app.exception_handler(deps.LoginRequired)
    async def _login_required(request: Request, exc: deps.LoginRequired):
        if deps.is_htmx(request):
            resp = JSONResponse({"detail": "login required"}, status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return resp
        return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        wants_html = "text/html" in request.headers.get("accept", "") or deps.is_htmx(request)
        if wants_html and exc.status_code in (403, 404):
            try:
                return deps.render(request, "error.html", {"status": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)
            except Exception:  # pragma: no cover
                pass
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    return app


app = create_app()
