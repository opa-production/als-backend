from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.core.clock import now as utc_now
from app.models.account import User
from app.schemas.account import DeleteAccountResponse, ProfileOut, ProfileUpdate
from app.services import auth as auth_service

router = APIRouter()


async def _profile(session: DbSession, user: User) -> ProfileOut:
    """
    The profile plus the entitlement behind it, in one query.

    The subscription is eager-loaded rather than fetched separately and stitched
    on afterwards. Every relationship on these models is ``lazy="raise"``, so a
    schema field sharing a relationship's name would blow up during validation
    the moment Pydantic reached for it — asking for it up front is both the fix
    and one fewer round trip.
    """
    loaded = await session.scalar(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.subscription))
    )

    return ProfileOut.model_validate(loaded or user)


@router.get("", response_model=ProfileOut, summary="Your profile")
async def read_me(user: CurrentUser, session: DbSession) -> ProfileOut:
    """
    Who you are, and what you are entitled to.

    The subscription travels with the profile so the app can reconcile the copy
    it keeps locally in one call rather than two — and so an expired plan is
    noticed on the next open rather than the next payment.
    """
    return await _profile(session, user)


@router.patch("", response_model=ProfileOut, summary="Update your profile")
async def update_me(
    payload: ProfileUpdate, user: CurrentUser, session: DbSession
) -> ProfileOut:
    """
    Changes only the fields present in the request.

    ``exclude_unset`` matters: without it a client sending just a name would
    blank the institution, the programme and the year, because Pydantic would
    hand over ``None`` for everything it was not told about.
    """
    changes = payload.model_dump(exclude_unset=True)

    if "email" in changes and changes["email"]:
        changes["email"] = changes["email"].strip().lower()

    for field, value in changes.items():
        setattr(user, field, value)

    await session.flush()
    return await _profile(session, user)


@router.delete("", response_model=DeleteAccountResponse, summary="Delete your account")
async def delete_me(user: CurrentUser, session: DbSession) -> DeleteAccountResponse:
    """
    Marks the account deleted and signs every device out.

    A tombstone rather than a hard delete, for two reasons. A device that has
    been offline needs to *hear* about the deletion — a row that simply
    vanished would be pushed straight back on the next sync. And a student who
    deletes an account by mistake at 2am before an exam has a window in which
    someone can still put it back.

    Files in Supabase are removed by the sweep that runs on the retention
    window, not here: a delete request should not block on object storage.
    """
    user.deleted_at = utc_now()
    await auth_service.revoke_device_tokens(session, user_id=user.id, device_id=None)
    await session.flush()

    return DeleteAccountResponse(
        message="Your account is scheduled for deletion and you have been signed out."
    )
