from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.session import dispose_engine, engine

log = structlog.get_logger()

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

    # Refuses only on the unsafe things — a default signing secret, DEBUG in
    # production. See app/core/config.py for why the missing-integration case
    # is a warning instead.
    settings.assert_production_ready()

    for missing in settings.unavailable_features():
        # `warning`, not `info`: these are the answers to support questions
        # that otherwise cost an afternoon, and they should be greppable in the
        # first screen of a container's logs.
        log.warning("feature_unavailable", detail=missing)

    if settings.review_account_configured:
        # Loud, and by name. One number that signs in with a fixed code and no
        # SMS is a real hole in the auth story — a deliberate one, for the app
        # store reviewers, but nobody should ever discover it by reading the
        # source. It is a warning so it sits with the other things worth
        # knowing in the first screen of the log.
        log.warning("review_account_enabled", phone=settings.review_phone)

    log.info(
        "started",
        environment=settings.environment,
        payments=settings.payments_configured,
        storage=settings.storage_configured,
        sms=settings.sms_configured,
        google_sign_in=bool(settings.google_client_ids),
    )

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

    @app.get("/ready", tags=["ops"], summary="Readiness")
    async def ready() -> JSONResponse:
        """
        Whether this process can actually serve — which means the database.

        Separate from ``/health`` on purpose. Liveness must not touch Postgres:
        a probe that does restarts every container the moment the database
        hiccups, turning a brief blip into an outage. Readiness is the opposite
        question — "should traffic come here right now" — and for that the
        database is exactly the thing to check.

        Deliberately says nothing about which integrations are configured. A
        deploy gate only needs to know whether to send traffic, and publishing
        the shape of the environment to anyone who asks is not worth the
        convenience. The admin console's Operations page has that, behind a
        login.
        """
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            # The class and message go to the journal, never to the response.
            # Logging only the event name once cost an afternoon: the service
            # reported `database: false` while the identical connection string
            # succeeded from a shell, and the one fact that would have told us
            # why — DNS failure, refused, bad password — was being discarded
            # here. The body stays deliberately bare; an unauthenticated probe
            # should not be able to read the database hostname back out.
            log.warning(
                "readiness_database_unreachable",
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "database": False},
            )

        return JSONResponse(content={"status": "ready", "database": True})

    return app


app = create_app()
