"""FastAPI application assembly.

Vercel serves the FastAPI instance named ``app`` (see api/index.py, which
re-exports it). The assembly logic lives here so the app imports easily in tests
without depending on the api/ directory.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.settings import production_webhook_config_errors
from app.store.redis import get_store
from app.telegram.webhook import router as webhook_router


def create_app() -> FastAPI:
    application = FastAPI(title="tg-llm-bot", version="0.1.0")
    application.include_router(webhook_router)

    @application.get("/api/health")
    def health() -> JSONResponse:
        on_vercel = bool(os.environ.get("VERCEL"))

        configuration_errors = production_webhook_config_errors() if on_vercel else []

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
            "warning": "; ".join(warnings) or None,
        }
        return JSONResponse(content=body, status_code=200 if ready else 503)

    return application


app = create_app()
