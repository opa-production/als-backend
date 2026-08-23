from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.session import dispose_engine

DESCRIPTION = """
Ardena Learning System.

The mobile app is local-first: it works with no server at all, and this API is
the durable copy plus the arbiter for anything a device cannot decide on its
own — payment, group seats, PDF text extraction.

Two things to know before using these endpoints:

* **Ids are minted by the client.** Writes are upserts on an id the device
  chose, so any request can be retried safely.
* **Files never pass through this API.** Uploads and downloads go straight to
  Supabase Storage through short-lived signed URLs.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Everything that must exist before the first request, and be released after
    the last one.

    The HTTP client is created once and shared. A client per request re-does
    TLS every time and leaks sockets until the process runs out — the classic
    way an async service degrades over hours rather than failing outright.
    """
    configure_logging()
    settings.assert_production_ready()

    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    yield

    await app.state.http.aclose()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ALS API",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Swagger is the test surface for now, so it stays on outside prod.
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    install_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health", tags=["ops"], summary="Liveness")
    async def health() -> dict[str, str]:
        """
        Deliberately does not touch the database.

        A liveness probe that queries Postgres restarts every container the
        moment the database hiccups, turning a brief blip into an outage.
        Readiness is a separate concern and gets its own endpoint later.
        """
        return {"status": "ok", "environment": settings.environment}

    return app


app = create_app()
