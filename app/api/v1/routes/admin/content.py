import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.models.knowledge import Material
from app.schemas.admin import AdminMaterialRow, ContentStatsOut, Page
from app.services import analytics

router = APIRouter()


@router.get("/stats", response_model=ContentStatsOut, summary="What is in the system")
async def stats(session: DbSession) -> ContentStatsOut:
    """
    Volume, and the health of the extraction pipeline.

    ``extraction`` is a count per status and ``extraction_stalled`` is the
    subset that has been ``pending`` for over an hour. The two together answer
    the only operational question this pipeline has: is the worker running, and
    is it keeping up.
    """
    return await analytics.content_stats(session)


@router.get(
    "/materials", response_model=Page[AdminMaterialRow], summary="Extraction queue"
)
async def materials(
    session: DbSession,
    extraction_status: str | None = Query(
        default=None, description="pending | running | done | failed | skipped"
    ),
    kind: str | None = Query(default=None, description="note | pdf | image | link"),
    user_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminMaterialRow]:
    """
    Material metadata, for chasing a stuck or failed extraction.

    Titles and page counts, never ``body`` and never the file. A console needs
    to know that a 240-page PDF failed and why; it does not need to be a reader
    for a student's coursework, and building it as one makes every future
    support session an unnecessary disclosure.
    """
    statement = select(Material).where(Material.deleted_at.is_(None))

    if extraction_status:
        statement = statement.where(Material.extraction_status == extraction_status)
    if kind:
        statement = statement.where(Material.kind == kind)
    if user_id:
        statement = statement.where(Material.user_id == user_id)

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.scalars(
            statement.order_by(Material.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    return Page(
        items=[
            AdminMaterialRow(
                id=row.id,
                user_id=row.user_id,
                unit_id=row.unit_id,
                kind=row.kind,
                title=row.title,
                byte_size=row.byte_size,
                page_count=row.page_count,
                extraction_status=row.extraction_status,
                extraction_error=row.extraction_error,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
