from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.errors import QuotaExceeded
from app.models.billing import Subscription, UsageCounter
from app.services.plans import UNLIMITED, Tier, limits_for, plan_for, unit_cap


@dataclass(frozen=True)
class Entitlement:
    """What a student may do right now, resolved once per request."""

    tier: Tier
    expires_at: datetime | None
    verified: bool
    #: The tier that was bought, before expiry was applied. Only for showing
    #: "your Synapse plan ended" — never for deciding what is allowed.
    nominal_tier: Tier = Tier.TRIAL

    @property
    def limits(self):
        return limits_for(self.tier)

    @property
    def is_expired(self) -> bool:
        return self.tier is Tier.EXPIRED


# --- Periods -----------------------------------------------------------------


def day_key(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m-%d")


def week_key(moment: datetime | None = None) -> str:
    """
    ISO week, Monday-first, matching the client's own week boundary.

    If the two disagreed, a student would see five quizzes left on the phone
    and be refused by the server, which reads as a bug rather than a limit.
    """
    return (moment or utc_now()).strftime("%G-W%V")


def month_key(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m")


#: Which period each metered thing rolls over on.
METRIC_PERIODS = {
    "ai_queries": day_key,
    "quizzes_weekly": week_key,
    "quizzes_lifetime": lambda _=None: "lifetime",
    "ocr_pages": month_key,
    "pdf_pages": lambda _=None: "lifetime",
}


# --- Entitlement -------------------------------------------------------------


async def get_entitlement(session: AsyncSession, user_id: uuid.UUID) -> Entitlement:
    """
    The tier in force **right now**, computed on every request.

    Expiry is evaluated here rather than by a nightly job. A job means a window
    — however short — in which a lapsed account still has a live tier in a
    column, and windows like that are what people find and share. Comparing a
    timestamp costs nothing and cannot be out of date.

    A lapsed subscription resolves to ``EXPIRED``, not back to the trial. The
    earlier behaviour was a loophole: pay for one month, lapse, and keep trial
    limits forever — a free tier nobody agreed to sell. Reads stay open, so
    nothing a student wrote is held hostage.

    An unverified paid subscription is treated as expired too. The app writes
    one optimistically when a student says they paid; until Kora confirms
    it, that is a claim, and a claim is not an entitlement.
    """
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )

    if subscription is None:
        # No row at all means the account never got a trial — which is what a
        # returning abuser looks like. Nothing, not a fresh fortnight.
        return Entitlement(
            tier=Tier.EXPIRED,
            expires_at=None,
            verified=False,
            nominal_tier=Tier.EXPIRED,
        )

    nominal = plan_for(subscription.tier).id
    expires = as_utc(subscription.expires_at)

    lapsed = expires is None or expires <= utc_now()
    unverified_paid = nominal is not Tier.TRIAL and not subscription.verified

    return Entitlement(
        tier=Tier.EXPIRED if (lapsed or unverified_paid) else nominal,
        expires_at=expires,
        verified=subscription.verified,
        nominal_tier=nominal,
    )


# --- Counters ----------------------------------------------------------------


async def _counter(
    session: AsyncSession, user_id: uuid.UUID, metric: str, period: str
) -> UsageCounter | None:
    return await session.scalar(
        select(UsageCounter).where(
            UsageCounter.user_id == user_id,
            UsageCounter.metric == metric,
            UsageCounter.period_key == period,
        )
    )


async def current_usage(
    session: AsyncSession, user_id: uuid.UUID, metric: str
) -> int:
    period = METRIC_PERIODS[metric]()
    row = await _counter(session, user_id, metric, period)
    return row.count if row else 0


async def record_usage(
    session: AsyncSession, user_id: uuid.UUID, metric: str, amount: int = 1
) -> int:
    """
    Adds to a counter, creating the period's row on first use.

    Read-then-write rather than an upsert, because the upsert syntax differs
    between Postgres and SQLite and this is not a hot enough path to justify
    two dialect-specific branches. Two concurrent requests from one student
    could interleave and lose a count — one question slipping through on a
    daily quota is a cost this does not need to pay a raced transaction to
    prevent.
    """
    period = METRIC_PERIODS[metric]()
    row = await _counter(session, user_id, metric, period)

    if row is not None:
        row.count += amount
        await session.flush()
        return row.count

    # First use of this metric in this period. Two concurrent requests both
    # reach here, and the unique constraint decides which one wins.
    #
    # The savepoint is the whole point. Without it the loser's INSERT raises
    # inside the *outer* transaction, and Postgres then refuses every
    # subsequent statement on that connection with
    #
    #     InFailedSQLTransactionError: current transaction is aborted,
    #     commands ignored until end of transaction block
    #
    # The traceback that surfaces names whatever query ran next -- a harmless
    # SELECT on usage_counters -- so the report points at the victim rather
    # than at the cause. A nested block rolls back to the savepoint instead,
    # leaving the surrounding transaction healthy.
    try:
        async with session.begin_nested():
            row = UsageCounter(
                user_id=user_id,
                metric=metric,
                period_key=period,
                count=amount,
                period_date=date.today(),
            )
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The other request created it. Read it back and add to it.
        row = await _counter(session, user_id, metric, period)
        if row is None:
            raise  # Not the collision we assumed; do not swallow it.
        row.count += amount
        await session.flush()

    return row.count


# --- Checks ------------------------------------------------------------------
#
# Each raises QuotaExceeded — a 402, not a 429. This is "not included in what
# you pay for", not "too fast", and the app shows a different screen for each.


async def check_unit_cap(
    session: AsyncSession, user_id: uuid.UUID, entitlement: Entitlement
) -> None:
    from app.models.course import Unit

    cap = unit_cap(entitlement.tier)
    used = await session.scalar(
        select(func.count())
        .select_from(Unit)
        .where(Unit.user_id == user_id, Unit.deleted_at.is_(None))
    )

    if (used or 0) >= cap:
        raise QuotaExceeded(
            f"{plan_for(entitlement.tier).name} covers {cap} course "
            f"{'unit' if cap == 1 else 'units'}."
        )


async def check_ai_query(
    session: AsyncSession, user_id: uuid.UUID, entitlement: Entitlement
) -> None:
    limit = entitlement.limits.daily_ai_queries
    if limit == UNLIMITED:
        return

    used = await current_usage(session, user_id, "ai_queries")
    if used >= limit:
        raise QuotaExceeded(f"You have used today's {limit} AI questions.")


async def check_quiz(
    session: AsyncSession, user_id: uuid.UUID, entitlement: Entitlement
) -> None:
    limits = entitlement.limits
    if limits.quiz_count == UNLIMITED or limits.quiz_interval == "unlimited":
        return

    metric = (
        "quizzes_weekly" if limits.quiz_interval == "weekly" else "quizzes_lifetime"
    )
    used = await current_usage(session, user_id, metric)

    if used >= limits.quiz_count:
        raise QuotaExceeded(
            f"You have used this week's {limits.quiz_count} quizzes."
            if limits.quiz_interval == "weekly"
            else f"{plan_for(entitlement.tier).name} includes "
            f"{limits.quiz_count} quizzes in total."
        )


def check_file_size(entitlement: Entitlement, byte_size: int | None) -> None:
    """
    Checked before a signed upload URL is issued, not after the bytes land.

    Afterwards is too late: the file is already in the bucket and already cost
    the student their data.
    """
    if not byte_size:
        return

    limit_mb = entitlement.limits.max_single_file_size_mb
    size_mb = byte_size / (1024 * 1024)

    if size_mb > limit_mb:
        raise QuotaExceeded(
            f"{plan_for(entitlement.tier).name} accepts files up to "
            f"{limit_mb}MB. That one is {size_mb:.1f}MB."
        )


async def check_pdf_pages(
    session: AsyncSession,
    user_id: uuid.UUID,
    entitlement: Entitlement,
    pages: int,
) -> None:
    """
    The per-file and total page limits, at last enforceable.

    Neither can be checked on the device — nothing there can open a PDF — so
    both are advertised on the pricing card and unchecked in the app. This runs
    after extraction, when the real page count is finally known.
    """
    limits = entitlement.limits

    if pages > limits.max_single_file_pages:
        raise QuotaExceeded(
            f"{plan_for(entitlement.tier).name} accepts files up to "
            f"{limits.max_single_file_pages} pages. That one has {pages}."
        )

    used = await current_usage(session, user_id, "pdf_pages")
    if used + pages > limits.total_pdf_pages_pool:
        remaining = max(0, limits.total_pdf_pages_pool - used)
        raise QuotaExceeded(
            f"That would use {pages} pages and you have {remaining} left of "
            f"{limits.total_pdf_pages_pool}."
        )


async def check_ocr(
    session: AsyncSession, user_id: uuid.UUID, entitlement: Entitlement, pages: int = 1
) -> None:
    limits = entitlement.limits
    if not limits.allow_ocr_scans:
        raise QuotaExceeded("Scanning handwritten notes is a Synapse feature.")

    used = await current_usage(session, user_id, "ocr_pages")
    if used + pages > limits.monthly_ocr_page_limit:
        raise QuotaExceeded(
            f"You have scanned this month's {limits.monthly_ocr_page_limit} pages."
        )


def new_period_end(tier: str | Tier, start: datetime | None = None) -> datetime:
    """When a subscription bought now would run out."""
    return (start or utc_now()) + timedelta(days=plan_for(tier).duration_days)
