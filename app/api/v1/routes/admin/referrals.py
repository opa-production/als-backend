"""
The referral programme, from the console.

Read-only. There is no endpoint here to grant a reward by hand, and that is
deliberate: the rules live in ``app/services/referrals.py`` and a second way to
put days on an account would be a second set of rules, kept in someone's head.
Support already has one — ``POST /admin/users/{id}/subscription`` — and it
demands a written reason, which a referral override should too.

What this is for is answering two questions. "My friend paid and I got
nothing", which the ledger answers by naming the rule that refused it. And "is
anybody gaming this", which the refusal counts answer better than any single
number could.
"""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.api.deps import DbSession
from app.models.account import User
from app.models.referral import ReferralReward
from app.schemas.admin import (
    AdminReferralRow,
    Page,
    ReferralStatsOut,
    TopReferrer,
)

router = APIRouter()

#: Aliased because both sides of a referral are `users`, and a self-join needs
#: to say which one it means.
_REFERRER = User.__table__.alias("referrer")
_REFERRED = User.__table__.alias("referred")


@router.get("/stats", response_model=ReferralStatsOut, summary="Is it working")
async def stats(session: DbSession) -> ReferralStatsOut:
    """
    The programme in one screen.

    Signups against payers is whether it works. The refusal breakdown is
    whether it is being played — a handful is the rules doing their job, and a
    spike in one reason is a person, not a trend.
    """
    referred_signups = (
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.referred_by_user_id.is_not(None), User.deleted_at.is_(None)
            )
        )
    ) or 0

    by_status = {
        status: int(count)
        for status, count in (
            await session.execute(
                select(ReferralReward.status, func.count()).group_by(
                    ReferralReward.status
                )
            )
        ).all()
    }

    by_reason = {
        reason: int(count)
        for reason, count in (
            await session.execute(
                select(ReferralReward.reason, func.count())
                .where(ReferralReward.status == "voided")
                .group_by(ReferralReward.reason)
            )
        ).all()
        if reason
    }

    days = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralReward.status == "credited", ReferralReward.days),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralReward.status == "banked", ReferralReward.days),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
        )
    ).one()

    # A referral that was refused is not a payer we won, so the count is of
    # rewards that still stand.
    referred_payers = (
        await session.scalar(
            select(func.count())
            .select_from(ReferralReward)
            .where(ReferralReward.status != "voided")
        )
    ) or 0

    leaders = (
        await session.execute(
            select(
                User.id,
                User.full_name,
                User.institution,
                func.count(ReferralReward.id),
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralReward.status == "credited", ReferralReward.days),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .join(ReferralReward, ReferralReward.referrer_id == User.id)
            .where(ReferralReward.status != "voided")
            .group_by(User.id, User.full_name, User.institution)
            .order_by(func.count(ReferralReward.id).desc())
            .limit(10)
        )
    ).all()

    return ReferralStatsOut(
        referred_signups=referred_signups,
        referred_payers=referred_payers,
        rewards_by_status=by_status,
        voided_by_reason=by_reason,
        days_credited=int(days[0] or 0),
        days_banked=int(days[1] or 0),
        top_referrers=[
            TopReferrer(
                user_id=row[0],
                full_name=row[1],
                institution=row[2],
                paid_referrals=int(row[3]),
                days_earned=int(row[4]),
            )
            for row in leaders
        ],
    )


@router.get("", response_model=Page[AdminReferralRow], summary="The ledger")
async def rewards(
    session: DbSession,
    status: str | None = Query(
        default=None, description="pending | banked | credited | voided"
    ),
    reason: str | None = Query(
        default=None,
        description="Only voided rows: self_referral | same_device | "
        "monthly_cap | bank_full | bank_expired",
    ),
    #: Matches either side on purpose -- see the docstring.
    user_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminReferralRow]:
    """
    Every reward, newest first, with both people named.

    ``user_id`` matches either side on purpose. A support thread starts with
    one account and the question is usually about the other, and asking the
    caller to know which end they are holding is asking them to guess.
    """
    statement = (
        select(
            ReferralReward,
            _REFERRER.c.full_name,
            _REFERRED.c.full_name,
        )
        .join(_REFERRER, _REFERRER.c.id == ReferralReward.referrer_id)
        .outerjoin(_REFERRED, _REFERRED.c.id == ReferralReward.referred_user_id)
    )

    if status:
        statement = statement.where(ReferralReward.status == status)
    if reason:
        statement = statement.where(ReferralReward.reason == reason)
    if user_id:
        statement = statement.where(
            (ReferralReward.referrer_id == user_id)
            | (ReferralReward.referred_user_id == user_id)
        )

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.execute(
            statement.order_by(ReferralReward.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page(
        items=[
            AdminReferralRow(
                id=reward.id,
                referrer_id=reward.referrer_id,
                referrer_name=referrer_name or "",
                referred_user_id=reward.referred_user_id,
                # Empty once that account is deleted. The row outlives it on
                # purpose: the days were earned and still have to be explained.
                referred_name=referred_name or "",
                status=reward.status,
                reason=reward.reason,
                days=reward.days,
                friend_days=reward.friend_days,
                tier=reward.tier,
                vest_at=reward.vest_at,
                banked_until=reward.banked_until,
                credited_at=reward.credited_at,
                created_at=reward.created_at,
            )
            for reward, referrer_name, referred_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
