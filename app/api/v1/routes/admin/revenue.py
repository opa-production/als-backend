from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.billing import Payment
from app.schemas.admin import (
    PlanRowOut,
    RevenueSummaryOut,
    SeriesPointOut,
    TimeseriesOut,
)
from app.services import analytics

router = APIRouter()


@router.get("/summary", response_model=RevenueSummaryOut, summary="Money, summarised")
async def summary(session: DbSession) -> RevenueSummaryOut:
    """
    Every headline revenue figure, in KES.

    Amounts are whole shillings, as stored. Paystack works in the smallest
    unit and the conversion already happened at the boundary in
    ``app/services/paystack.py``; doing it again here is how a dashboard ends
    up a hundred times wrong in a way nobody notices while the numbers are
    small.

    ``growth_30d_pct`` is null rather than zero when there is no prior period.
    A first month has no growth rate, and inventing one puts a fake number on
    the front page.
    """
    return await analytics.revenue_summary(session)


@router.get("/by-plan", response_model=list[PlanRowOut], summary="Revenue per plan")
async def by_plan(session: DbSession) -> list[PlanRowOut]:
    """
    What each plan brings in, and how many people are on it.

    Read ``mrr_ksh`` for Friends carefully: it is counted once per group, not
    once per seat. See ``app/services/analytics.py``.
    """
    return await analytics.plan_breakdown(session)


@router.get("/timeseries", response_model=TimeseriesOut, summary="Revenue over time")
async def revenue_timeseries(
    session: DbSession,
    days: int = Query(default=30, ge=1, le=365),
    metric: str = Query(
        default="revenue", description="revenue | payments | failed_payments"
    ),
) -> TimeseriesOut:
    if metric not in ("revenue", "payments", "failed_payments"):
        metric = "revenue"

    points = await analytics.timeseries(session, metric=metric, days=days)
    return TimeseriesOut(
        metric=metric,
        days=days,
        points=[SeriesPointOut(day=point.day, value=point.value) for point in points],
        total=sum(point.value for point in points),
    )


@router.get("/top-customers", summary="Who has spent the most")
async def top_customers(
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    days: int | None = Query(
        default=None, ge=1, le=365, description="Window. Omit for all time."
    ),
) -> list[dict]:
    """
    Lifetime value, descending.

    A short list on a subscription product with three price points, and worth
    having anyway: the accounts at the top are the ones whose renewal lapsing
    is worth an email rather than a dashboard.
    """
    conditions = [Payment.status == "success"]
    if days:
        conditions.append(Payment.created_at >= utc_now() - timedelta(days=days))

    rows = (
        await session.execute(
            select(
                User.id,
                User.full_name,
                User.phone,
                User.institution,
                func.coalesce(func.sum(Payment.amount_kes), 0).label("total"),
                func.count(Payment.id).label("payments"),
            )
            .join(Payment, Payment.user_id == User.id)
            .where(*conditions)
            .group_by(User.id, User.full_name, User.phone, User.institution)
            .order_by(func.coalesce(func.sum(Payment.amount_kes), 0).desc())
            .limit(limit)
        )
    ).all()

    return [
        {
            "user_id": str(row.id),
            "full_name": row.full_name,
            "phone": row.phone,
            "institution": row.institution,
            "total_paid_ksh": int(row.total or 0),
            "payments": int(row.payments),
        }
        for row in rows
    ]
