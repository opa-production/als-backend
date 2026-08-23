import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    """
    An error with a message meant for a student.

    Anything raised as one of these is safe to show in the app. Everything else
    is treated as a bug and reported as a generic failure, because an
    exception string is as likely to contain a table name as an explanation.
    """

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN


class QuotaExceeded(AppError):
    """
    A plan limit stopped the request.

    402 rather than 429: this is not "too fast", it is "not included in what
    you pay for", and the client shows a different screen for each.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED


def _envelope(message: str, status_code: int) -> JSONResponse:
    """
    The shape the app already expects.

    ``src/api/client.js`` reads ``message`` off a failed response and shows it,
    so every error leaves here in the same shape regardless of what raised it.
    """
    return JSONResponse(status_code=status_code, content={"message": message})


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(exc.message, exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _envelope(str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's error list is precise and unreadable. The first field is
        # almost always the one the caller got wrong.
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
        return _envelope(
            f"{field}: {first.get('msg', 'is not valid')}",
            # Starlette renamed this; the number is the same either way.
            getattr(
                status,
                "HTTP_422_UNPROCESSABLE_CONTENT",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # A constraint fired. The detail names columns and constraints, so it
        # is logged and not returned.
        log.warning("integrity_error", path=request.url.path, error=str(exc.orig))
        return _envelope(
            "That conflicts with something already saved.", status.HTTP_409_CONFLICT
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return _envelope(
            "Something went wrong on our side.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
