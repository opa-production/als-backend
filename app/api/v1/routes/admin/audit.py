import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.models.admin import AdminAuditLog
from app.schemas.admin import AuditRow, Page

router = APIRouter()


@router.get("", response_model=Page[AuditRow], summary="What admins have done")
async def list_audit(
    session: DbSession,
    action: str | None = Query(
        default=None, description="Exact match, e.g. subscription.granted"
    ),
    admin_id: uuid.UUID | None = None,
    target_id: Annotated[
        uuid.UUID | None,
        Query(description="Everything ever done to one user or payment."),
    ] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Page[AuditRow]:
    """
    The log, newest first.

    Readable by any admin including ``support``, and deliberately so: a log
    only the people who can edit it can read is not much of a check on them.
    There is no write path — entries are appended by the actions themselves and
    nothing in this API deletes or edits one.

    ``target_id`` is the filter that gets used: given a user id, it returns
    every administrative thing that has ever happened to that account, which is
    the first question a disputed charge or a mysteriously free plan raises.
    """
    statement = select(AdminAuditLog)

    if action:
        statement = statement.where(AdminAuditLog.action == action)
    if admin_id:
        statement = statement.where(AdminAuditLog.admin_id == admin_id)
    if target_id:
        statement = statement.where(AdminAuditLog.target_id == target_id)
    if since:
        statement = statement.where(AdminAuditLog.created_at >= since)
    if until:
        statement = statement.where(AdminAuditLog.created_at <= until)

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.scalars(
            statement.order_by(AdminAuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page(
        items=[AuditRow.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/actions", summary="Which action names exist")
async def actions(session: DbSession) -> list[dict]:
    """
    Distinct actions and how often each has happened.

    Populates the filter dropdown without hard-coding a list in the front end
    that would then drift from what the backend actually writes.
    """
    rows = (
        await session.execute(
            select(AdminAuditLog.action, func.count())
            .group_by(AdminAuditLog.action)
            .order_by(func.count().desc())
        )
    ).all()

    return [{"action": action, "count": int(count)} for action, count in rows]
