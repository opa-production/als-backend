import logging
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings

#: Carries the request id across every ``await`` in a request without threading
#: it through call signatures. A plain global would be shared by every
#: concurrent request on the worker; a ContextVar is per-task, which is what
#: makes it correct under async.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def _add_request_id(logger, method_name, event_dict):
    request_id = request_id_var.get()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging() -> None:
    """
    JSON in production, readable in development.

    Grep does not help across a dozen containers — the logs have to be
    machine-parsable for anything to be findable. Locally that is just noise,
    so development gets the console renderer.
    """
    processors = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    processors.append(
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.debug else logging.INFO
        ),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        level=logging.DEBUG if settings.debug else logging.INFO,
    )
    # SQLAlchemy and uvicorn.access are noisy and duplicate what the middleware
    # below records once, with more context.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Tags every request with an id and records how long it took.

    The id is echoed back in ``X-Request-Id`` so a student reporting a problem
    can be traced to the exact log line, and an inbound one is honoured so a
    trace survives a proxy in front of this service.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        log = structlog.get_logger()

        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-Id"] = request_id

        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
