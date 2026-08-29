from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger()


def _explain(response: httpx.Response, action: str, **context: object) -> None:
    """
    Record what the storage API actually said.

    Only the status code was logged before, which is close to useless here:
    Supabase answers 400 for a missing bucket, a bad path, an expired key and a
    duplicate object alike, and the difference is entirely in the body. Chasing
    "Could not start the upload (400)" meant reproducing it by hand against the
    API, which is the sort of thing this log exists to make unnecessary.

    The body is truncated and goes to the journal only. It can name the bucket
    and the object path, which are not for the caller.
    """
    detail = ""
    try:
        detail = response.text[:300]
    except Exception:  # a streamed or already-closed response
        detail = "<unreadable>"

    log.warning(
        "storage_request_failed",
        action=action,
        status=response.status_code,
        detail=detail,
        **context,
    )


class Bucket(StrEnum):
    """
    The four buckets, and nothing else.

    Every one is **private**. A public bucket means a leaked path is a
    permanent leak with no way to revoke it, and student coursework is not
    something to hand out on a guessable URL.
    """

    MATERIALS = "materials"
    SCANS = "scans"
    AVATARS = "avatars"
    EXPORTS = "exports"


#: What each bucket will accept, checked before a signed upload URL is issued.
#: Enforcing at the point of signing is what stops the bucket becoming general
#: file hosting for anyone who has ever held a token.
ALLOWED_MIME: dict[Bucket, frozenset[str]] = {
    Bucket.MATERIALS: frozenset({"application/pdf"}),
    Bucket.SCANS: frozenset({"image/jpeg", "image/png", "image/heic", "image/webp"}),
    Bucket.AVATARS: frozenset({"image/jpeg", "image/png", "image/webp"}),
    Bucket.EXPORTS: frozenset({"application/pdf"}),
}

#: Hard ceilings, independent of the plan. The plan limits sit *below* these
#: and are checked separately; this is the "no matter who you are" line.
#:
#: Every one of these must stay at or under the bucket's own limit in Supabase,
#: which is 50MB. A ceiling above it is worse than no ceiling: the file passes
#: the check here, a URL is signed, the device uploads the whole thing, and
#: Supabase refuses it at the end. The point of checking before signing is that
#: a refusal costs the student nothing, and that is lost the moment these two
#: numbers disagree. Materials and exports sat at 60MB and did exactly that.
MAX_BYTES: dict[Bucket, int] = {
    Bucket.MATERIALS: 50 * 1024 * 1024,
    Bucket.SCANS: 25 * 1024 * 1024,
    Bucket.AVATARS: 5 * 1024 * 1024,
    Bucket.EXPORTS: 50 * 1024 * 1024,
}


@dataclass(frozen=True)
class SignedUpload:
    """A URL the client PUTs to directly, and the path to record afterwards."""

    url: str
    path: str
    token: str


@dataclass(frozen=True)
class StorageError(Exception):
    message: str


def object_path(
    *,
    user_id: uuid.UUID,
    material_id: uuid.UUID,
    extension: str,
    unit_id: uuid.UUID | None = None,
) -> str:
    """
    Where a file lives, and who it belongs to.

    The user id is the first segment on purpose: it makes ownership a property
    of the path, so a storage policy can be written as "the prefix must match
    the caller" rather than as a lookup. It also makes deleting an account a
    prefix delete instead of a walk over every row.
    """
    tail = f"{unit_id}/{material_id}" if unit_id else str(material_id)
    suffix = extension.lstrip(".").lower()
    return f"{user_id}/{tail}.{suffix}"


class SupabaseStorage:
    """
    Thin adapter over Supabase Storage's REST API.

    Deliberately not the ``supabase-py`` SDK: it is sync, so every call would
    block the event loop, and the three operations this service needs are one
    HTTP request each.

    **Bytes never pass through this process.** Uploads get a signed URL the
    client PUTs to directly, and downloads get a signed URL the client GETs.
    Proxying a 50 MB slide deck would occupy a worker for the length of a
    student's upload on campus wifi, which is the fastest way to make a healthy
    API look dead.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._base = settings.supabase_url.rstrip("/")

    def _assert_configured(self) -> None:
        """
        Fails with a sentence rather than a puzzle.

        Without this, an unset ``SUPABASE_URL`` leaves ``_base`` empty and every
        call goes to a relative path — httpx rejects it with a
        ``UnsupportedProtocol`` that names neither Supabase nor the missing
        variable, and the student sees "something went wrong on our side".
        """
        if not settings.storage_configured:
            raise StorageError(
                "File storage is not configured on this server yet."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "apikey": settings.supabase_service_key,
        }

    async def signed_upload_url(self, bucket: Bucket, path: str) -> SignedUpload:
        """A short-lived URL the client uploads to. Overwrites are rejected."""
        self._assert_configured()
        response = await self._client.post(
            f"{self._base}/storage/v1/object/upload/sign/{bucket}/{path}",
            headers=self._headers,
        )
        if response.status_code >= 400:
            _explain(response, "signed_upload_url", bucket=str(bucket), path=path)
            raise StorageError(f"Could not start the upload ({response.status_code}).")

        payload = response.json()
        return SignedUpload(
            url=f"{self._base}/storage/v1{payload['url']}",
            path=path,
            token=payload.get("token", ""),
        )

    async def signed_download_url(
        self, bucket: Bucket, path: str, ttl_seconds: int | None = None
    ) -> str:
        """
        A URL that expires.

        The TTL is short by default and the URL is minted per request, so a
        link pasted into a group chat stops working long before it spreads.
        """
        self._assert_configured()
        ttl = ttl_seconds or settings.supabase_storage_signed_url_ttl
        response = await self._client.post(
            f"{self._base}/storage/v1/object/sign/{bucket}/{path}",
            headers=self._headers,
            json={"expiresIn": ttl},
        )
        if response.status_code >= 400:
            _explain(response, "signed_download_url")
            raise StorageError(f"Could not sign that file ({response.status_code}).")

        return f"{self._base}/storage/v1{response.json()['signedURL']}"

    async def delete(self, bucket: Bucket, paths: list[str]) -> None:
        """
        Removes objects for good.

        Called when a material is *hard* deleted, never on a soft delete — a
        tombstoned row still has to be able to come back.
        """
        self._assert_configured()
        if not paths:
            return

        response = await self._client.request(
            "DELETE",
            f"{self._base}/storage/v1/object/{bucket}",
            headers=self._headers,
            json={"prefixes": paths},
        )
        if response.status_code >= 400:
            _explain(response, "delete")
            raise StorageError(f"Could not delete those files ({response.status_code}).")
