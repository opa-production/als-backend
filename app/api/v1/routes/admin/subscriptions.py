from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.api.deps import DbSession
from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.billing import Subscription
from app.schemas.admin import (
    AdminSubscriptionRow,
    Page,
    SubscriptionStatsOut,
)
from app.services import analytics
from app.services.plans import Tier, plan_for

router = APIRouter()


@router.get("", response_model=Page[AdminSubscriptionRow], summary="All subscriptions")
async def list_subscriptions(
    session: DbSession,
    tier: str | None = Query(default=None, description="trial | standard | pro | friends"),
    verified: bool | None = None,
    active: bool | None = Query(
        default=None, description="True for entitled right now, False for lapsed."
    ),
    expiring_days: int | None = Query(
        default=None,
        ge=1,
        le=90,
        description="Only plans running out within this many days.",
    ),
    q: str | None = Query(
        default=None, description="Matches the holder's name, phone or email."
    ),
    sort: str = Query(default="expires_at", description="expires_at | started_at"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AdminSubscriptionRow]:
    """
    Subscriptions with their holder attached.

    Default ordering is by expiry ascending, because the useful question is
    almost always "who is about to lapse" and not "who signed up recently" —
    the latter is what the users table is for.

    ``verified=false`` combined with a paid tier is the reconciliation queue:
    every row is a student the app believes is paying and Paystack has never
    confirmed.
    """
    now = utc_now()
    statement = (
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(User.deleted_at.is_(None))
    )

    if tier:
        statement = statement.where(Subscription.tier == tier)

    if verified is not None:
        statement = statement.where(Subscription.verified.is_(verified))

    if active is True:
        statement = statement.where(*analytics.active_subscription_filter(now))
    elif active is False:
        statement = statement.where(
            or_(
                Subscription.expires_at.is_(None),
                Subscription.expires_at <= now,
                Subscription.verified.is_(False),
            )
        )

    if expiring_days is not None:
        statement = statement.where(
            Subscription.expires_at.isnot(None),
            Subscription.expires_at > now,
            Subscription.expires_at <= now + timedelta(days=expiring_days),
        )

    if q:
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(User.full_name).like(pattern),
                func.lower(User.phone).like(pattern),
                func.lower(User.email).like(pattern),
            )
        )

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    column = {
        "expires_at": Subscription.expires_at,
        "started_at": Subscription.started_at,
    }.get(sort, Subscription.expires_at)

    rows = (
        await session.execute(
            statement.order_by(column.desc() if order == "desc" else column.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = []
    for subscription, user in rows:
        expires = as_utc(subscription.expires_at)
        items.append(
            AdminSubscriptionRow(
                id=subscription.id,
                user_id=user.id,
                full_name=user.full_name,
                phone=user.phone,
                email=user.email,
                tier=subscription.tier,
                plan_name=plan_for(subscription.tier).name,
                started_at=subscription.started_at,
                expires_at=subscription.expires_at,
                verified=subscription.verified,
                is_active=bool(
                    subscription.verified and expires is not None and expires > now
                ),
                days_remaining=(
                    max(0, (expires - now).days) if expires is not None else None
                ),
                group_id=subscription.group_id,
            )
        )

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/stats", response_model=SubscriptionStatsOut, summary="Customers per plan"
)
async def subscription_stats(session: DbSession) -> SubscriptionStatsOut:
    """
    How many people are on each plan, and what each plan is worth.

    ``total_paying`` counts people; ``mrr_ksh`` counts money, and the two do
    not divide into each other for the Friends tier — five seats, one payment.
    Both figures are correct and they are different questions.
    """
    now = utc_now()
    plans = await analytics.plan_breakdown(session)

    return SubscriptionStatsOut(
        generated_at=now,
        plans=plans,
        total_active=await analytics.count_rows(
            session, Subscription, *analytics.active_subscription_filter(now)
        ),
        total_paying=sum(row.paying for row in plans),
        total_trial=await analytics.count_rows(
            session,
            Subscription,
            Subscription.tier == Tier.TRIAL.value,
            *analytics.active_subscription_filter(now),
        ),
        total_expired=await analytics.count_rows(
            session,
            Subscription,
            or_(
                Subscription.expires_at.is_(None),
                Subscription.expires_at <= now,
            ),
        ),
        total_unverified=await analytics.count_rows(
            session,
            Subscription,
            Subscription.tier.in_(analytics.paid_tiers()),
            Subscription.verified.is_(False),
            Subscription.expires_at > now,
        ),
        mrr_ksh=sum(row.mrr_ksh for row in plans),
    )
