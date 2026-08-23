import uuid
from typing import Annotated

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.account import User

#: ``auto_error=False`` so a missing header reaches the handler below and comes
#: back in the app's ``{"message": ...}`` shape, rather than FastAPI's own
#: ``{"detail": ...}`` which the client does not read.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_http_client(request: Request) -> httpx.AsyncClient:
    """
    The one client created in the lifespan.

    Taken from app state rather than made per call: a new client re-does TLS
    every request and leaks sockets until the process runs out of them, which
    is how an async service degrades over hours instead of failing outright.
    """
    return request.app.state.http


HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


async def get_current_user(
    session: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> User:
    """
    The account behind the bearer token.

    The token carries only an id, so the user is loaded fresh on every request.
    That costs one indexed primary-key lookup and means a deleted account or a
    changed profile takes effect immediately rather than at the next sign-in.
    """
    unauthorised = AppError("Please sign in again.", status_code=401)

    if credentials is None or not credentials.credentials:
        raise unauthorised

    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise unauthorised

    # JWT claims are strings; the column is a real UUID. Passing the string
    # through works on some drivers and fails on others, which is the worst
    # kind of bug — one that only shows up on a backend nobody tested against.
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise unauthorised from None

    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise unauthorised

    # One live session per account, enforced on every request rather than only
    # at refresh. A token issued to a device that has since been replaced stops
    # working immediately instead of lasting out its thirty minutes.
    device_id = claims.get("did")
    if (
        device_id
        and user.active_device_id
        and str(user.active_device_id) != device_id
    ):
        raise AppError(
            "You have signed in on another device.", status_code=401
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_device_id(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> uuid.UUID | None:
    """The device this token was issued to, when it named one."""
    if credentials is None:
        return None
    claims = decode_access_token(credentials.credentials)
    if not claims or not claims.get("did"):
        return None

    try:
        return uuid.UUID(claims["did"])
    except ValueError:
        return None
