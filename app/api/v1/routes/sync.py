from datetime import datetime

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.schemas.sync import SyncPull, SyncPush, SyncPushResult
from app.services import sync as sync_service
from app.services.quota import get_entitlement

router = APIRouter()

#: Module-level so it is built once, not on every request — and so ruff stops
#: warning about a function call in a default argument, which is the real
#: hazard it is pointing at.
SinceCursor = Query(
    default=None,
    description="Cursor from the previous sync. Omit on a first run to get everything.",
)


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
    since: datetime | None = SinceCursor,
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
