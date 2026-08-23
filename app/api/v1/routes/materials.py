import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.errors import AppError, NotFound
from app.models.course import Unit
from app.models.knowledge import Material
from app.services.quota import check_file_size, get_entitlement
from app.services.storage import (
    ALLOWED_MIME,
    MAX_BYTES,
    Bucket,
    StorageError,
    SupabaseStorage,
    object_path,
)

router = APIRouter()


class UploadUrlRequest(BaseModel):
    #: Minted by the device, so the material row and the object share an id and
    #: a retried upload overwrites rather than duplicating.
    material_id: uuid.UUID
    unit_id: uuid.UUID
    kind: str = Field(pattern="^(pdf|image)$")
    filename: str = Field(max_length=255)
    mime_type: str = Field(max_length=128)
    byte_size: int = Field(gt=0)


class UploadUrlResponse(BaseModel):
    upload_url: str
    bucket: str
    path: str
    token: str


class CompleteUploadRequest(BaseModel):
    material_id: uuid.UUID
    title: str = Field(max_length=300)


class MaterialStatus(BaseModel):
    id: uuid.UUID
    extraction_status: str
    page_count: int | None = None
    message: str


class DownloadUrlResponse(BaseModel):
    url: str
    expires_in: int


def _bucket_for(kind: str) -> Bucket:
    return Bucket.MATERIALS if kind == "pdf" else Bucket.SCANS


@router.post(
    "/upload-url", response_model=UploadUrlResponse, summary="Start a file upload"
)
async def create_upload_url(
    payload: UploadUrlRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> UploadUrlResponse:
    """
    Signs a URL the device uploads to directly.

    **The bytes never pass through this API.** Proxying a 50 MB slide deck
    would tie up a worker for the length of a student's upload on campus wifi,
    which is the fastest way to make a healthy service look dead.

    Everything that can be refused is refused *here*, before a byte moves:
    plan file-size limit, bucket ceiling, and content type. Checking afterwards
    is too late — the file is already in the bucket and has already cost the
    student their data.
    """
    bucket = _bucket_for(payload.kind)

    if payload.mime_type not in ALLOWED_MIME[bucket]:
        raise AppError(f"{payload.mime_type} files cannot be added here.")

    if payload.byte_size > MAX_BYTES[bucket]:
        ceiling = MAX_BYTES[bucket] // (1024 * 1024)
        raise AppError(f"Files must be under {ceiling}MB.")

    entitlement = await get_entitlement(session, user.id)
    check_file_size(entitlement, payload.byte_size)

    unit = await session.scalar(
        select(Unit).where(Unit.id == payload.unit_id, Unit.user_id == user.id)
    )
    if unit is None or unit.deleted_at is not None:
        raise NotFound("That unit no longer exists.")

    extension = payload.filename.rsplit(".", 1)[-1] if "." in payload.filename else "bin"
    path = object_path(
        user_id=user.id,
        unit_id=payload.unit_id,
        material_id=payload.material_id,
        extension=extension,
    )

    storage = SupabaseStorage(http)
    try:
        signed = await storage.signed_upload_url(bucket, path)
    except StorageError as error:
        raise AppError(error.message) from error

    # The row is written now, before the upload, so an upload that never
    # completes leaves a visible `pending` record rather than an orphaned
    # object nobody can find.
    material = await session.get(Material, payload.material_id)
    if material is None:
        material = Material(
            id=payload.material_id,
            user_id=user.id,
            unit_id=payload.unit_id,
            kind=payload.kind,
            title=payload.filename,
        )
        session.add(material)

    material.storage_bucket = bucket.value
    material.storage_path = path
    material.mime_type = payload.mime_type
    material.byte_size = payload.byte_size
    material.extraction_status = "pending"
    await session.flush()

    return UploadUrlResponse(
        upload_url=signed.url, bucket=bucket.value, path=path, token=signed.token
    )


@router.post(
    "/complete", response_model=MaterialStatus, summary="Confirm an upload landed"
)
async def complete_upload(
    payload: CompleteUploadRequest, user: CurrentUser, session: DbSession
) -> MaterialStatus:
    """
    Marks the file as arrived and queues text extraction.

    Extraction does not run here. Opening a PDF is CPU-bound, and doing it in a
    handler blocks the event loop — which stalls *every* other request on the
    process, not just this one. The row moves to `pending` and a worker picks
    it up.

    Until that worker exists the status stays `pending`, which is honest: the
    file is stored and the text is not searchable yet.
    """
    material = await session.get(Material, payload.material_id)
    if material is None or material.user_id != user.id:
        raise NotFound("That file is not on your account.")

    material.title = payload.title
    material.extraction_status = "pending"
    await session.flush()

    return MaterialStatus(
        id=material.id,
        extraction_status=material.extraction_status,
        page_count=material.page_count,
        message="Uploaded. Its text will be searchable once it has been read.",
    )


@router.get(
    "/{material_id}/download-url",
    response_model=DownloadUrlResponse,
    summary="Get a link to a file",
)
async def create_download_url(
    material_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> DownloadUrlResponse:
    """
    A short-lived signed URL for the stored file.

    Minted per request and never stored. Buckets are private, so a link pasted
    into a group chat stops working long before it spreads — which is the whole
    reason not to serve coursework from a public bucket.
    """
    material = await session.get(Material, material_id)
    if material is None or material.user_id != user.id or material.deleted_at:
        raise NotFound("That file is not on your account.")

    if not material.storage_path or not material.storage_bucket:
        raise NotFound("That item has no file attached.")

    storage = SupabaseStorage(http)
    try:
        url = await storage.signed_download_url(
            Bucket(material.storage_bucket), material.storage_path
        )
    except StorageError as error:
        raise AppError(error.message) from error

    from app.core.config import settings

    return DownloadUrlResponse(
        url=url, expires_in=settings.supabase_storage_signed_url_ttl
    )
