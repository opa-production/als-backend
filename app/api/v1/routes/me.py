import uuid

import structlog
from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.clock import now as utc_now
from app.core.config import settings
from app.core.errors import AppError, NotFound
from app.models.account import User
from app.schemas.account import (
    AvatarUploadUrlRequest,
    AvatarUploadUrlResponse,
    AvatarUrlResponse,
    ConfirmAvatarRequest,
    DeleteAccountResponse,
    ProfileOut,
    ProfileUpdate,
)
from app.services import auth as auth_service
from app.services.storage import (
    ALLOWED_MIME,
    MAX_BYTES,
    Bucket,
    StorageError,
    SupabaseStorage,
    object_path,
)

log = structlog.get_logger()

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


def _owns(user: User, path: str) -> bool:
    """
    Whether this storage path belongs to this student.

    `object_path` puts the user id first for exactly this: ownership is
    readable from the path, with no lookup and nothing to forget.
    """
    return path.startswith(f"{user.id}/")


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


# --- The profile photo --------------------------------------------------------
#
# Three calls, the same shape as a material upload: sign, upload direct to
# Supabase, confirm. The bytes never pass through this API.
#
# It was two calls short of working. `users.avatar_path` and the `avatars`
# bucket have both existed since the first migration, and `PATCH /me` would
# happily store a path in that column -- but nothing ever minted a URL to
# upload to, and nothing ever signed one to read back. So the app showed the
# photo the picker had just handed it, sent a path pointing at an object that
# was never written, and lost the picture at the next sign-in when the profile
# came back from the server with nothing in it. The bucket stayed empty while
# `materials` filled up, which is exactly what was reported.


@router.post(
    "/avatar/upload-url",
    response_model=AvatarUploadUrlResponse,
    summary="Start a profile photo upload",
)
async def create_avatar_upload_url(
    payload: AvatarUploadUrlRequest,
    user: CurrentUser,
    http: HttpClient,
) -> AvatarUploadUrlResponse:
    """
    Signs a URL the device uploads the photo to directly.

    Refused here, before a byte moves: content type and size. Afterwards is too
    late — the object is already in the bucket and has already cost the student
    their data.

    A fresh object id every time, rather than a stable `avatar.jpg`. Supabase
    rejects an overwrite through a signed upload URL, so a stable path would
    make the second photo a student ever set fail; and a changing path is also
    what stops a cached copy of the old one being served in place of the new.
    The previous object is deleted on confirmation.
    """
    if payload.mime_type not in ALLOWED_MIME[Bucket.AVATARS]:
        raise AppError("A profile photo has to be a JPEG, PNG or WebP.")

    if payload.byte_size > MAX_BYTES[Bucket.AVATARS]:
        ceiling = MAX_BYTES[Bucket.AVATARS] // (1024 * 1024)
        raise AppError(f"A profile photo has to be under {ceiling}MB.")

    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }[payload.mime_type]

    path = object_path(
        user_id=user.id, material_id=uuid.uuid4(), extension=extension
    )

    storage = SupabaseStorage(http)
    try:
        signed = await storage.signed_upload_url(Bucket.AVATARS, path)
    except StorageError as error:
        raise AppError(error.message) from error

    return AvatarUploadUrlResponse(
        upload_url=signed.url, bucket=Bucket.AVATARS.value, path=path, token=signed.token
    )


@router.post("/avatar", response_model=ProfileOut, summary="Confirm a profile photo")
async def confirm_avatar(
    payload: ConfirmAvatarRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> ProfileOut:
    """
    Records the uploaded photo as this student's, and removes the one before it.

    The path is checked against the caller rather than trusted. `object_path`
    puts the user id first precisely so ownership is a property of the path, and
    without this check a client could name somebody else's object and then read
    it back through the signed download below — which would turn a profile
    photo into a way to look at another student's.

    Deleting the old object is best-effort. A student whose new photo is saved
    and whose old file lingers has a tidy-up problem; one whose save fails
    because the tidy-up did has a broken feature.
    """
    if not _owns(user, payload.path):
        raise AppError("That is not your upload.", status_code=403)

    previous = user.avatar_path
    user.avatar_path = payload.path
    await session.flush()

    if previous and previous != payload.path:
        try:
            await SupabaseStorage(http).delete(Bucket.AVATARS, [previous])
        except StorageError as error:
            log.warning(
                "avatar_cleanup_failed", user_id=str(user.id), error=error.message
            )

    return await _profile(session, user)


@router.get("/avatar-url", response_model=AvatarUrlResponse, summary="Your photo")
async def read_avatar_url(
    user: CurrentUser, http: HttpClient
) -> AvatarUrlResponse:
    """
    A short-lived signed URL for the stored photo.

    Minted per request and never stored, like every other download here: the
    bucket is private, and a URL kept in a profile response is a link that
    stops working while the app still believes in it.
    """
    if not user.avatar_path:
        raise NotFound("You have not set a profile photo.")

    storage = SupabaseStorage(http)
    try:
        url = await storage.signed_download_url(Bucket.AVATARS, user.avatar_path)
    except StorageError as error:
        raise AppError(error.message) from error

    return AvatarUrlResponse(
        url=url, expires_in=settings.supabase_storage_signed_url_ttl
    )


@router.delete("/avatar", response_model=ProfileOut, summary="Remove your photo")
async def delete_avatar(
    user: CurrentUser, session: DbSession, http: HttpClient
) -> ProfileOut:
    """Clears the photo, and the object behind it."""
    previous = user.avatar_path
    user.avatar_path = None
    await session.flush()

    if previous:
        try:
            await SupabaseStorage(http).delete(Bucket.AVATARS, [previous])
        except StorageError as error:
            log.warning(
                "avatar_cleanup_failed", user_id=str(user.id), error=error.message
            )

    return await _profile(session, user)
