import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.api.deps import DbSession
from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.errors import NotFound
from app.models.account import User
from app.models.billing import PlanGroup, PlanGroupMember
from app.schemas.admin import (
    AdminGroupDetail,
    AdminGroupMember,
    AdminGroupRow,
    Page,
)

router = APIRouter()


async def _seat_counts(session, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """
    Seats taken for a page of groups, in one query.

    The obvious version calls ``seats_taken`` per row, which is fifty queries
    for a fifty-row table — the classic N+1, and the classic place it hides is
    a list view that was tested with three rows.
    """
    if not group_ids:
        return {}

    rows = (
        await session.execute(
            select(PlanGroupMember.group_id, func.count())
            .where(PlanGroupMember.group_id.in_(group_ids))
            .group_by(PlanGroupMember.group_id)
        )
    ).all()
    return {group_id: int(count) for group_id, count in rows}


@router.get("", response_model=Page[AdminGroupRow], summary="Friends plans")
async def list_groups(
    session: DbSession,
    active: bool | None = Query(default=None, description="Filter by whether it still runs."),
    q: str | None = Query(default=None, description="Matches invite code or owner."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminGroupRow]:
    """
    Every group plan, with its owner and how full it is.

    Worth its own screen because a group is the one place where one payment and
    several entitlements come apart. "Five people on Synapse limits, one
    payment of 1,250" is correct here and looks like a discrepancy everywhere
    else.
    """
    now = utc_now()
    statement = select(PlanGroup, User).join(User, User.id == PlanGroup.owner_id)

    if active is True:
        statement = statement.where(
            PlanGroup.expires_at.isnot(None), PlanGroup.expires_at > now
        )
    elif active is False:
        statement = statement.where(
            or_(PlanGroup.expires_at.is_(None), PlanGroup.expires_at <= now)
        )

    if q:
        needle = q.strip()
        pattern = f"%{needle.lower()}%"
        statement = statement.where(
            or_(
                func.upper(PlanGroup.invite_code) == needle.upper(),
                func.lower(User.full_name).like(pattern),
                func.lower(User.phone).like(pattern),
            )
        )

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.execute(
            statement.order_by(PlanGroup.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    seats = await _seat_counts(session, [group.id for group, _ in rows])

    items = [
        AdminGroupRow(
            id=group.id,
            owner_id=owner.id,
            owner_name=owner.full_name,
            owner_phone=owner.phone,
            tier=group.tier,
            invite_code=group.invite_code,
            seats=group.seats,
            seats_taken=seats.get(group.id, 0),
            expires_at=group.expires_at,
            is_active=bool(
                (expires := as_utc(group.expires_at)) is not None and expires > now
            ),
            created_at=group.created_at,
        )
        for group, owner in rows
    ]

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{group_id}", response_model=AdminGroupDetail, summary="One group")
async def get_group(group_id: uuid.UUID, session: DbSession) -> AdminGroupDetail:
    group = await session.get(PlanGroup, group_id)
    if group is None:
        raise NotFound("No group with that id.")

    owner = await session.get(User, group.owner_id)

    rows = (
        await session.execute(
            select(PlanGroupMember, User)
            .join(User, User.id == PlanGroupMember.user_id)
            .where(PlanGroupMember.group_id == group_id)
            .order_by(PlanGroupMember.created_at)
        )
    ).all()

    expires = as_utc(group.expires_at)
    now = utc_now()

    return AdminGroupDetail(
        id=group.id,
        owner_id=group.owner_id,
        owner_name=owner.full_name if owner else "",
        owner_phone=owner.phone if owner else None,
        tier=group.tier,
        invite_code=group.invite_code,
        seats=group.seats,
        seats_taken=len(rows),
        expires_at=group.expires_at,
        is_active=bool(expires is not None and expires > now),
        created_at=group.created_at,
        members=[
            AdminGroupMember(
                user_id=member.user_id,
                full_name=user.full_name,
                phone=user.phone,
                is_owner=member.user_id == group.owner_id,
                joined_at=member.created_at,
            )
            for member, user in rows
        ],
    )
