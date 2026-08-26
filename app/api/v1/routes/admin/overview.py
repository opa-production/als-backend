from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.models.account import User
from app.models.billing import Payment, Subscription
from app.schemas.admin import (
    AttentionItem,
    OverviewOut,
    SeriesPointOut,
    TimeseriesOut,
)
from app.services import analytics

router = APIRouter()


async def _attention(session: AsyncSession) -> list[AttentionItem]:
    """
    The short list of things that want a person.

    Each entry is a question the console can answer with a number and a link,
    and each is here because it is silent otherwise. An unverified paid
    subscription in particular never surfaces on its own: the student sees a
    working plan, the API sees an unpaid one, and nothing complains until
    someone opens a ticket.
    """
    now = utc_now()
    items: list[AttentionItem] = []

    unverified = await analytics.count_rows(
        session,
        Subscription,
        Subscription.tier.in_(analytics.paid_tiers()),
        Subscription.verified.is_(False),
        Subscription.expires_at > now,
    )
    if unverified:
        items.append(
            AttentionItem(
                level="critical",
                code="unverified_paid_subscriptions",
                message=(
                    f"{unverified} paid subscription"
                    f"{'' if unverified == 1 else 's'} never confirmed by "
                    "Paystack. Each is either a lost payment or a free plan."
                ),
                count=unverified,
                link="/subscriptions?verified=false",
            )
        )

    pending = await analytics.count_rows(
        session,
        Payment,
        Payment.status == "pending",
        Payment.created_at < now - timedelta(hours=1),
    )
    if pending:
        items.append(
            AttentionItem(
                level="warn",
                code="stale_pending_payments",
                message=(
                    f"{pending} payment{'' if pending == 1 else 's'} still "
                    "pending after an hour. Reconcile against Paystack."
                ),
                count=pending,
                link="/payments?status=pending",
            )
        )

    content = await analytics.content_stats(session)
    if content.extraction_stalled:
        items.append(
            AttentionItem(
                level="warn",
                code="extraction_stalled",
                message=(
                    f"{content.extraction_stalled} material"
                    f"{'' if content.extraction_stalled == 1 else 's'} waiting "
                    "over an hour for text extraction."
                ),
                count=content.extraction_stalled,
                link="/content/materials?extraction_status=pending",
            )
        )

    failed = content.extraction.get("failed", 0)
    if failed:
        items.append(
            AttentionItem(
                level="info",
                code="extraction_failed",
                message=f"{failed} material{'' if failed == 1 else 's'} failed extraction.",
                count=failed,
                link="/content/materials?extraction_status=failed",
            )
        )

    expiring = await analytics.count_rows(
        session,
        Subscription,
        Subscription.tier.in_(analytics.paid_tiers()),
        *analytics.active_subscription_filter(now),
        Subscription.expires_at <= now + timedelta(days=3),
    )
    if expiring:
        items.append(
            AttentionItem(
                level="info",
                code="expiring_soon",
                message=(
                    f"{expiring} paid plan{'' if expiring == 1 else 's'} expire "
                    "within three days."
                ),
                count=expiring,
                link="/subscriptions?expiring_days=3",
            )
        )

    return items


@router.get("/overview", response_model=OverviewOut, summary="Dashboard")
async def overview(session: DbSession) -> OverviewOut:
    """
    Everything the front page shows, in one request.

    One round trip rather than six, because a dashboard that fires six requests
    renders in six stages and every one of them can fail on its own. The
    queries inside are cheap counts and sums; if that ever stops being true the
    answer is a cached view behind this same shape, not a chattier console.
    """
    return OverviewOut(
        generated_at=utc_now(),
        users=await analytics.user_counts(session),
        revenue=await analytics.revenue_summary(session),
        plans=await analytics.plan_breakdown(session),
        funnel=await analytics.funnel(session),
        attention=await _attention(session),
    )


@router.get(
    "/overview/timeseries", response_model=TimeseriesOut, summary="One metric over time"
)
async def timeseries(
    session: DbSession,
    metric: str = Query(
        default="signups",
        description="signups | revenue | payments | failed_payments | materials "
        "| questions | active_students",
    ),
    days: int = Query(default=30, ge=1, le=365),
) -> TimeseriesOut:
    """
    A dense daily series — every day in the window is present, zeros included.

    The gaps are the point. A chart that omits the empty days draws a straight
    line through the weekend the payment provider was down.
    """
    try:
        points = await analytics.timeseries(session, metric=metric, days=days)
    except KeyError:
        raise AppError(
            f"Unknown metric. Try one of: {', '.join(sorted(analytics.SERIES))}."
        ) from None

    return TimeseriesOut(
        metric=metric,
        days=days,
        points=[SeriesPointOut(day=point.day, value=point.value) for point in points],
        total=sum(point.value for point in points),
    )


@router.get(
    "/overview/institutions",
    summary="Where the students are",
)
async def institutions(
    session: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> list[dict]:
    """
    Signups grouped by institution.

    Blank institutions are excluded rather than bucketed as "Unknown": the
    field is free text a student may never have filled in, and a chart whose
    biggest bar is "did not answer" tells you about the form, not the market.
    """
    rows = (
        await session.execute(
            select(User.institution, func.count())
            .where(User.deleted_at.is_(None), User.institution != "")
            .group_by(User.institution)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()

    return [{"institution": name, "users": int(count)} for name, count in rows]
