"""FastAPI application assembly.

Vercel serves the FastAPI instance named ``app`` (see api/index.py, which
re-exports it). The assembly logic lives here so the app imports easily in tests
without depending on the api/ directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.settings import production_config_errors, settings
from app.store.redis import get_store
from app.telegram.processor import router as processor_router
from app.telegram.webhook import router as webhook_router
from app.admin.routes import router as admin_router


def create_app() -> FastAPI:
    application = FastAPI(title="tg-llm-bot", version="0.1.0")

    @application.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: https:; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://oauth.telegram.org")
        if os.environ.get("VERCEL"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path in {"/", "/index.html"} or request.url.path.startswith(
            ("/api/auth/", "/api/admin/")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response
    application.include_router(webhook_router)
    application.include_router(processor_router)
    application.include_router(admin_router)

    @application.get("/api/health")
    def health() -> JSONResponse:
        on_vercel = bool(os.environ.get("VERCEL"))

        configuration_errors = production_config_errors() if on_vercel else []

        backend = "unavailable"
        try:
            store = get_store()
            backend = store.backend()
            store_reachable = bool(store.ping())
        except Exception:
            store_reachable = False

        persistent = backend == "upstash"
        ready = store_reachable and not configuration_errors and (persistent or not on_vercel)
        warnings: list[str] = []
        if configuration_errors:
            warnings.append(
                "missing or invalid production settings: "
                + ", ".join(configuration_errors)
            )
        if on_vercel and not persistent:
            warnings.append("persistent Upstash Redis is required on Vercel")
        if not store_reachable:
            warnings.append("configured store did not answer PING")

        body = {
            "ok": ready,
            "service": "tg-llm-bot",
            "store": backend,
            "dependencies": {
                "tavily": "enabled" if bool(settings.TAVILY_API_KEY) else "disabled"
            },
            "warning": "; ".join(warnings) or None,
        }
        return JSONResponse(content=body, status_code=200 if ready else 503)

    public_dir = Path(__file__).resolve().parent.parent / "public"
    if public_dir.exists():
        application.mount("/", StaticFiles(directory=public_dir, html=True), name="public")

    return application


app = create_app()
