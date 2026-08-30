"""
Reading what students have asked for.

Read-only, deliberately. There is no status to set and no reply to write,
because a triage workflow nobody keeps up to date is worse than a plain list —
it makes a stale board look like a decision. What these rows are for is
deciding what to build next; that decision is recorded in the roadmap, not in a
column here.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.billing import Subscription
from app.models.feedback import FeatureRequest
from app.schemas.admin import AdminFeatureRequestRow, Page
from app.services.quota import effective_tier

router = APIRouter()


@router.get(
    "/feature-requests",
    response_model=Page[AdminFeatureRequestRow],
    summary="What students have asked for",
)
async def feature_requests(
    session: DbSession,
    user_id: uuid.UUID | None = None,
    search: str | None = Query(
        default=None, description="Match against the text of the request."
    ),
    days: int | None = Query(
        default=None, ge=1, le=365, description="Only the last N days."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminFeatureRequestRow]:
    """
    Every request, newest first, with who asked.

    The requester's plan is resolved from the subscription joined here rather
    than looked up per row: a page of fifty would otherwise be fifty extra
    queries to draw one column.

    ``search`` is a plain case-insensitive substring, not full-text. The
    question this answers is "has anyone else asked for offline mode", over a
    table measured in thousands of rows, and an index that has to be maintained
    to answer it faster than a scan is not worth having yet.
    """
    statement = (
        select(FeatureRequest, User, Subscription)
        .join(User, User.id == FeatureRequest.user_id)
        .outerjoin(Subscription, Subscription.user_id == User.id)
    )

    if user_id:
        statement = statement.where(FeatureRequest.user_id == user_id)
    if search:
        statement = statement.where(FeatureRequest.body.ilike(f"%{search}%"))
    if days:
        statement = statement.where(
            FeatureRequest.created_at > utc_now() - timedelta(days=days)
        )

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.execute(
            statement.order_by(FeatureRequest.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page(
        items=[
            AdminFeatureRequestRow(
                id=request.id,
                user_id=request.user_id,
                body=request.body,
                app_version=request.app_version,
                platform=request.platform,
                created_at=request.created_at,
                full_name=user.full_name,
                institution=user.institution,
                tier=effective_tier(subscription).value,
            )
            for request, user, subscription in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
