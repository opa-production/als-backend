from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BeforeValidator

from app.api.deps import CurrentUser, DbSession
from app.schemas.sync import SyncPull, SyncPush, SyncPushResult
from app.services import sync as sync_service
from app.services.quota import get_entitlement

router = APIRouter()

def _blank_is_no_cursor(value: object) -> object:
    """
    An empty `since` means "no cursor", not "a malformed date".

    A client with nothing stored yet naturally builds `?since=` from a null
    field, and rejecting that fails the *first* sync a device ever attempts --
    the one run where there is genuinely nothing to sync from. The error it
    produced said `input too short`, which points at the date parser rather
    than at the missing cursor, so it reads as a corrupt request.

    Every client would otherwise have to special-case omitting the parameter.
    Being liberal here costs nothing: an empty cursor has exactly one sensible
    reading.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


#: Module-level so it is built once, not on every request — and so ruff stops
#: warning about a function call in a default argument, which is the real
#: hazard it is pointing at.
SinceCursor = Annotated[
    datetime | None,
    BeforeValidator(_blank_is_no_cursor),
    Query(
        description=(
            "Cursor from the previous sync. Omit, or leave empty, on a first "
            "run to get everything."
        ),
    ),
]


@router.post("", response_model=SyncPushResult, summary="Push local changes")
async def push(
    payload: SyncPush, user: CurrentUser, session: DbSession
) -> SyncPushResult:
    """
    Writes everything the device changed while it was away.

    **Safe to retry.** Ids are minted on the device, so the same push applied
    twice is one set of rows, not two.

    **Last write wins**, compared on each row's `updated_at`. A row the server
    already has a newer copy of is counted in `skipped` rather than treated as
    an error — the device simply had stale data.

    One rejected row never fails the request. A student over their unit cap
    still needs the rest of their notes to sync, so refusals come back per
    table in `rejected` and everything else is applied.
    """
    entitlement = await get_entitlement(session, user.id)
    return await sync_service.push(
        session, user_id=user.id, payload=payload, entitlement=entitlement
    )


@router.get("", response_model=SyncPull, summary="Pull remote changes")
async def pull(
    user: CurrentUser,
    session: DbSession,
    since: SinceCursor = None,
) -> SyncPull:
    """
    Everything that changed after `since`, deletions included.

    Deleted rows come back with `deleted_at` set rather than being absent. A
    row that simply vanished would be invisible to a device that has been
    offline, which would then push it straight back.

    When `has_more` is true the page hit its limit: pull again with the
    returned cursor.
    """
    return await sync_service.pull(session, user_id=user.id, since=since)
