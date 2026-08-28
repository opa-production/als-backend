"""
The numbers the console shows.

Every figure here is computed from the tables that already exist — there is no
rollup table and no nightly job. That is a deliberate trade: an aggregate table
is faster and is also a second copy of the truth that goes wrong quietly, and
"quietly wrong revenue" is the one failure mode worth paying a few hundred
milliseconds to avoid. When a ``COUNT(*)`` over payments stops being cheap, the
fix is a materialised view with a known refresh time, not a counter incremented
in application code.

Two definitions, used consistently and stated once:

* **Active** — ``expires_at`` is in the future and ``verified`` is true. The
  same test ``app/services/quota.py`` applies before letting anyone spend a
  quota, so the console cannot say "1,204 paying" while the API is refusing
  1,204 people.
* **Paying** — active *and* on a tier that costs money. A trial is active and
  is not a customer, and conflating the two is how a free tier turns into a
  growth chart.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now as utc_now
from app.models.account import Device, User
from app.models.billing import (
    Payment,
    PlanGroup,
    PlanGroupMember,
    Subscription,
    UsageCounter,
)
from app.models.course import ClassSession, Unit
from app.models.knowledge import Material, MaterialChunk
from app.models.planner import Event
from app.models.settings import StudyDay
from app.models.tutor import Chat, Message
from app.services.plans import PLANS, SELLABLE, Tier, plan_for

#: What everything is normalised to when a monthly figure is quoted. Every plan
#: sold today runs 30 days, so this is currently a no-op — it exists so that
#: adding an annual plan does not silently multiply MRR by twelve.
MONTH_DAYS = 30

SUCCESS = "success"


# --- Small helpers -----------------------------------------------------------


async def _count(session: AsyncSession, statement: Select) -> int:
    return (await session.scalar(statement)) or 0


def _count_of(model, *conditions) -> Select:
    statement = select(func.count()).select_from(model)
    return statement.where(*conditions) if conditions else statement


async def count_rows(session: AsyncSession, model, *conditions) -> int:
    """
    ``SELECT COUNT(*)`` over one table, with conditions.

    Public because the route layer legitimately needs it — the attention
    banner on the dashboard counts three different tables and does not need
    three near-identical functions here to do it.
    """
    return await _count(session, _count_of(model, *conditions))


def _day_string(value: object) -> str:
    """
    One bucket key, whatever the driver returned.

    ``func.date()`` gives a ``date`` on Postgres and a string on SQLite. Both
    are correct; only one of them is JSON, so both become the ISO string here
    rather than at four separate call sites.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()[:10]
    return str(value)[:10]


def active_subscription_filter(moment: datetime | None = None):
    """The clause that means "entitled right now", in one place."""
    moment = moment or utc_now()
    return (
        Subscription.verified.is_(True),
        Subscription.expires_at.isnot(None),
        Subscription.expires_at > moment,
    )


def paid_tiers() -> list[str]:
    return [tier.value for tier in SELLABLE]


# --- Users -------------------------------------------------------------------


@dataclass(frozen=True)
class UserCounts:
    total: int
    #: Soft-deleted accounts, kept out of every other figure here.
    deleted: int
    new_today: int
    new_7d: int
    new_30d: int
    #: Has at least one registered device — the closest thing to "reachable"
    #: this schema can honestly answer.
    with_devices: int


async def user_counts(session: AsyncSession) -> UserCounts:
    now = utc_now()
    live = User.deleted_at.is_(None)

    return UserCounts(
        total=await _count(session, _count_of(User, live)),
        deleted=await _count(session, _count_of(User, User.deleted_at.isnot(None))),
        new_today=await _count(
            session,
            _count_of(User, live, User.created_at >= now - timedelta(days=1)),
        ),
        new_7d=await _count(
            session,
            _count_of(User, live, User.created_at >= now - timedelta(days=7)),
        ),
        new_30d=await _count(
            session,
            _count_of(User, live, User.created_at >= now - timedelta(days=30)),
        ),
        with_devices=await _count(
            session, select(func.count(func.distinct(Device.user_id)))
        ),
    )


# --- Plans -------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRow:
    tier: str
    name: str
    price_ksh: int
    #: Everyone holding this tier, live or lapsed.
    subscribers: int
    #: Holding it *and* entitled — the number that matters.
    active: int
    #: Active and paid for. Zero for trial, by definition.
    paying: int
    #: Written by the app on a student's word and never confirmed by Kora.
    #: A non-zero number here is a reconciliation job, not a statistic.
    unverified: int
    expiring_7d: int
    revenue_all_time_ksh: int
    revenue_30d_ksh: int
    mrr_ksh: int


async def plan_breakdown(session: AsyncSession) -> list[PlanRow]:
    """
    One row per plan: who is on it, and what it earns.

    The Friends plan is the reason this is not one ``GROUP BY``. A single
    payment of KES 1,250 creates up to five subscriptions with ``tier =
    friends``, so summing the plan price across subscribers would report five
    times the money that exists. Recurring revenue for that tier is counted per
    *group*; the seats are still counted as people, because they are people.
    """
    now = utc_now()

    async def tier_counts(tier: Tier) -> dict[str, int]:
        held = Subscription.tier == tier.value
        return {
            "subscribers": await _count(session, _count_of(Subscription, held)),
            "active": await _count(
                session,
                _count_of(Subscription, held, *active_subscription_filter(now)),
            ),
            "unverified": await _count(
                session,
                _count_of(
                    Subscription,
                    held,
                    Subscription.verified.is_(False),
                    Subscription.expires_at > now,
                ),
            ),
            "expiring_7d": await _count(
                session,
                _count_of(
                    Subscription,
                    held,
                    *active_subscription_filter(now),
                    Subscription.expires_at <= now + timedelta(days=7),
                ),
            ),
        }

    revenue_by_tier = {
        str(tier): int(total or 0)
        for tier, total in (
            await session.execute(
                select(Payment.tier, func.coalesce(func.sum(Payment.amount_kes), 0))
                .where(Payment.status == SUCCESS)
                .group_by(Payment.tier)
            )
        ).all()
    }
    revenue_30d_by_tier = {
        str(tier): int(total or 0)
        for tier, total in (
            await session.execute(
                select(Payment.tier, func.coalesce(func.sum(Payment.amount_kes), 0))
                .where(
                    Payment.status == SUCCESS,
                    Payment.created_at >= now - timedelta(days=30),
                )
                .group_by(Payment.tier)
            )
        ).all()
    }

    #: Live Friends groups, for the per-group revenue described above.
    live_groups = await _count(
        session,
        _count_of(
            PlanGroup, PlanGroup.expires_at.isnot(None), PlanGroup.expires_at > now
        ),
    )

    rows: list[PlanRow] = []
    for tier in (Tier.TRIAL, *SELLABLE):
        plan = PLANS[tier]
        counts = await tier_counts(tier)

        if tier is Tier.FRIENDS:
            billable = live_groups
        elif tier is Tier.TRIAL:
            billable = 0
        else:
            #: A seat borrowed from someone else's group does not bill. Only a
            #: subscription standing on its own pays a subscription price.
            billable = await _count(
                session,
                _count_of(
                    Subscription,
                    Subscription.tier == tier.value,
                    Subscription.group_id.is_(None),
                    *active_subscription_filter(now),
                ),
            )

        duration = max(1, plan.duration_days)
        rows.append(
            PlanRow(
                tier=tier.value,
                name=plan.name,
                price_ksh=plan.price_ksh,
                subscribers=counts["subscribers"],
                active=counts["active"],
                paying=0 if tier is Tier.TRIAL else counts["active"],
                unverified=counts["unverified"],
                expiring_7d=counts["expiring_7d"],
                revenue_all_time_ksh=revenue_by_tier.get(tier.value, 0),
                revenue_30d_ksh=revenue_30d_by_tier.get(tier.value, 0),
                mrr_ksh=int(billable * plan.price_ksh * MONTH_DAYS / duration),
            )
        )

    return rows


# --- Revenue -----------------------------------------------------------------


@dataclass(frozen=True)
class RevenueSummary:
    currency: str
    gross_ksh: int
    today_ksh: int
    last_7d_ksh: int
    last_30d_ksh: int
    previous_30d_ksh: int
    #: Percent, positive or negative. None when there is no previous period to
    #: compare against — growth against zero is not 100%, it is undefined, and
    #: printing "+100%" in the first month is a lie the chart will repeat.
    growth_30d_pct: float | None
    mrr_ksh: int
    arpu_ksh: int
    paying_customers: int
    successful_payments: int
    failed_payments: int
    pending_payments: int
    #: Successful charges over all attempts. The number that says whether
    #: M-Pesa is having a bad day.
    success_rate_pct: float
    average_payment_ksh: int
    by_channel: dict[str, int] = field(default_factory=dict)


async def _sum_payments(session: AsyncSession, *conditions) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount_kes), 0)).where(
            Payment.status == SUCCESS, *conditions
        )
    )
    return int(total or 0)


async def revenue_summary(session: AsyncSession) -> RevenueSummary:
    now = utc_now()

    gross = await _sum_payments(session)
    today = await _sum_payments(session, Payment.created_at >= now - timedelta(days=1))
    last_7 = await _sum_payments(session, Payment.created_at >= now - timedelta(days=7))
    last_30 = await _sum_payments(
        session, Payment.created_at >= now - timedelta(days=30)
    )
    previous_30 = await _sum_payments(
        session,
        Payment.created_at >= now - timedelta(days=60),
        Payment.created_at < now - timedelta(days=30),
    )

    status_counts = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(Payment.status, func.count()).group_by(Payment.status)
            )
        ).all()
    }
    succeeded = status_counts.get(SUCCESS, 0)
    attempted = sum(status_counts.values())

    by_channel = {
        (channel or "unknown"): int(total or 0)
        for channel, total in (
            await session.execute(
                select(Payment.channel, func.coalesce(func.sum(Payment.amount_kes), 0))
                .where(Payment.status == SUCCESS)
                .group_by(Payment.channel)
            )
        ).all()
    }

    plans = await plan_breakdown(session)
    mrr = sum(row.mrr_ksh for row in plans)
    paying = sum(row.paying for row in plans)

    return RevenueSummary(
        currency="KES",
        gross_ksh=gross,
        today_ksh=today,
        last_7d_ksh=last_7,
        last_30d_ksh=last_30,
        previous_30d_ksh=previous_30,
        growth_30d_pct=(
            round((last_30 - previous_30) / previous_30 * 100, 1)
            if previous_30
            else None
        ),
        mrr_ksh=mrr,
        arpu_ksh=int(last_30 / paying) if paying else 0,
        paying_customers=paying,
        successful_payments=succeeded,
        failed_payments=status_counts.get("failed", 0)
        + status_counts.get("abandoned", 0),
        pending_payments=status_counts.get("pending", 0),
        success_rate_pct=round(succeeded / attempted * 100, 1) if attempted else 0.0,
        average_payment_ksh=int(gross / succeeded) if succeeded else 0,
        by_channel=by_channel,
    )


# --- Time series -------------------------------------------------------------

#: What the console can plot. Each entry is (model, date column, value
#: expression, extra filters) — one table, so a new series is one line rather
#: than a new endpoint.
SERIES: dict[str, tuple] = {
    "signups": (User, User.created_at, func.count(), (User.deleted_at.is_(None),)),
    "revenue": (
        Payment,
        Payment.created_at,
        func.coalesce(func.sum(Payment.amount_kes), 0),
        (Payment.status == SUCCESS,),
    ),
    "payments": (
        Payment,
        Payment.created_at,
        func.count(),
        (Payment.status == SUCCESS,),
    ),
    "failed_payments": (
        Payment,
        Payment.created_at,
        func.count(),
        (Payment.status.in_(("failed", "abandoned")),),
    ),
    "materials": (Material, Material.created_at, func.count(), ()),
    "questions": (
        Message,
        Message.created_at,
        func.count(),
        (Message.role == "student",),
    ),
    "active_students": (
        StudyDay,
        StudyDay.created_at,
        func.count(func.distinct(StudyDay.user_id)),
        (),
    ),
}


@dataclass(frozen=True)
class SeriesPoint:
    day: str
    value: int


async def timeseries(
    session: AsyncSession, *, metric: str, days: int = 30
) -> list[SeriesPoint]:
    """
    A dense daily series — every day present, including the empty ones.

    Grouped in SQL, gap-filled in Python, rather than a recursive date CTE. The
    CTE is more elegant and is also Postgres-specific, and this same query has
    to run against the SQLite the test suite uses. The empty days are the ones
    a chart most needs: a line that skips them slopes straight through the gap
    and hides the outage that made it.
    """
    if metric not in SERIES:
        raise KeyError(metric)

    model, date_column, value, conditions = SERIES[metric]
    since = utc_now() - timedelta(days=days)
    bucket = func.date(date_column).label("day")

    rows = (
        await session.execute(
            select(bucket, value)
            .select_from(model)
            .where(date_column >= since, *conditions)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    found = {_day_string(day): int(total or 0) for day, total in rows}
    start = (utc_now() - timedelta(days=days - 1)).date()

    return [
        SeriesPoint(day=key, value=found.get(key, 0))
        for key in (
            (start + timedelta(days=offset)).isoformat() for offset in range(days)
        )
    ]


# --- Funnel ------------------------------------------------------------------


@dataclass(frozen=True)
class Funnel:
    signed_up: int
    started_trial: int
    trial_active: int
    trial_expired: int
    ever_paid: int
    paying_now: int
    #: Of everyone whose trial has run out, the share who then bought
    #: something. Anyone still inside their fourteen days is excluded — they
    #: have not decided yet, and counting them as a "no" makes every healthy
    #: week of signups look like a drop in conversion.
    trial_conversion_pct: float
    #: Of everyone who has ever paid, the share still entitled. The blunt
    #: retention number, and blunt is right until there is enough history for
    #: a cohort chart to say anything true.
    retention_pct: float


async def funnel(session: AsyncSession) -> Funnel:
    now = utc_now()

    signed_up = await _count(session, _count_of(User, User.deleted_at.is_(None)))
    started_trial = await _count(session, _count_of(Subscription))

    # The field names still say "trial" because the console reads them, but
    # what they count is the free floor: the trial is not granted any more, so
    # a funnel keyed on it would report zero for ever.
    #
    # `trial_active` is everyone sitting on free right now -- the pool a sale
    # can come out of. `trial_expired` is everyone who paid at some point and
    # has since dropped back to it, which is the honest replacement for "had a
    # trial and let it run out": both are people who have seen the product and
    # are not paying today.
    trial_active = await _count(
        session,
        _count_of(Subscription, Subscription.tier == Tier.FREE.value),
    )
    trial_expired = await _count(
        session,
        _count_of(
            Subscription,
            Subscription.tier.in_(paid_tiers()),
            Subscription.expires_at <= now,
        ),
    )

    ever_paid = await _count(
        session,
        select(func.count(func.distinct(Payment.user_id))).where(
            Payment.status == SUCCESS
        ),
    )
    paying_now = await _count(
        session,
        _count_of(
            Subscription,
            Subscription.tier.in_(paid_tiers()),
            *active_subscription_filter(now),
        ),
    )

    decided = trial_expired + ever_paid
    return Funnel(
        signed_up=signed_up,
        started_trial=started_trial,
        trial_active=trial_active,
        trial_expired=trial_expired,
        ever_paid=ever_paid,
        paying_now=paying_now,
        trial_conversion_pct=(
            round(ever_paid / decided * 100, 1) if decided else 0.0
        ),
        retention_pct=round(paying_now / ever_paid * 100, 1) if ever_paid else 0.0,
    )


# --- Content and pipeline ----------------------------------------------------


@dataclass(frozen=True)
class ContentStats:
    units: int
    materials: int
    material_chunks: int
    storage_bytes: int
    events: int
    class_sessions: int
    chats: int
    messages: int
    tutor_answers: int
    prompt_tokens: int
    completion_tokens: int
    extraction: dict[str, int]
    #: Anything sitting in ``pending`` for over an hour. The worker is either
    #: down or wedged, and this is the number that says which.
    extraction_stalled: int


async def content_stats(session: AsyncSession) -> ContentStats:
    now = utc_now()
    live = Material.deleted_at.is_(None)

    extraction = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(Material.extraction_status, func.count())
                .where(live)
                .group_by(Material.extraction_status)
            )
        ).all()
    }

    tokens = (
        await session.execute(
            select(
                func.coalesce(func.sum(Message.prompt_tokens), 0),
                func.coalesce(func.sum(Message.completion_tokens), 0),
            )
        )
    ).one()

    return ContentStats(
        units=await _count(session, _count_of(Unit, Unit.deleted_at.is_(None))),
        materials=await _count(session, _count_of(Material, live)),
        material_chunks=await _count(session, _count_of(MaterialChunk)),
        storage_bytes=int(
            await session.scalar(
                select(func.coalesce(func.sum(Material.byte_size), 0)).where(live)
            )
            or 0
        ),
        events=await _count(session, _count_of(Event, Event.deleted_at.is_(None))),
        class_sessions=await _count(
            session, _count_of(ClassSession, ClassSession.deleted_at.is_(None))
        ),
        chats=await _count(session, _count_of(Chat, Chat.deleted_at.is_(None))),
        messages=await _count(session, _count_of(Message)),
        tutor_answers=await _count(
            session, _count_of(Message, Message.role == "tutor")
        ),
        prompt_tokens=int(tokens[0] or 0),
        completion_tokens=int(tokens[1] or 0),
        extraction=extraction,
        extraction_stalled=await _count(
            session,
            _count_of(
                Material,
                live,
                Material.extraction_status == "pending",
                Material.created_at < now - timedelta(hours=1),
            ),
        ),
    )


# --- The tutor pipeline -------------------------------------------------------


@dataclass(frozen=True)
class AiHealth:
    """
    Whether the tutor could answer well, if it were being asked.

    Reports the *pipeline*, not a model. There is no LLM adapter yet — see
    ROADMAP.md — so anything claiming to measure inference latency here would be
    inventing it. What is genuinely knowable is whether the material a tutor
    would have to quote from has been read, chunked and made searchable, and
    that is what decides answer quality long before the model does.

    ``coverage_pct`` is the number to watch. A tutor that cites pages can only
    cite pages it has extracted; every failed extraction is a document a student
    can see in their library and get no answers from.
    """

    #: Whether /tutor/ask is served at all.
    tutor_available: bool
    #: Why not, when it is not. Empty when it is.
    tutor_status: str

    extractable: int
    extracted: int
    pending: int
    failed: int
    stalled: int
    coverage_pct: float

    chunks: int
    chunks_per_material: float

    answers_30d: int
    prompt_tokens_30d: int
    completion_tokens_30d: int
    avg_tokens_per_answer: int


async def ai_health(session: AsyncSession) -> AiHealth:
    now = utc_now()
    since_30d = now - timedelta(days=30)
    live = Material.deleted_at.is_(None)

    # `note` and `link` carry their text already and are never extracted, so
    # counting them would dilute coverage with documents that need no work.
    extractable_filter = (live, Material.kind.in_(("pdf", "image")))

    extractable = await count_rows(session, Material, *extractable_filter)
    extracted = await count_rows(
        session, Material, *extractable_filter, Material.extraction_status == "done"
    )
    pending = await count_rows(
        session,
        Material,
        *extractable_filter,
        Material.extraction_status.in_(("pending", "running")),
    )
    failed = await count_rows(
        session, Material, *extractable_filter, Material.extraction_status == "failed"
    )
    stalled = await count_rows(
        session,
        Material,
        *extractable_filter,
        Material.extraction_status == "pending",
        Material.created_at < now - timedelta(hours=1),
    )

    chunks = await count_rows(session, MaterialChunk)

    answers = await count_rows(
        session, Message, Message.role == "tutor", Message.created_at >= since_30d
    )
    tokens = (
        await session.execute(
            select(
                func.coalesce(func.sum(Message.prompt_tokens), 0),
                func.coalesce(func.sum(Message.completion_tokens), 0),
            ).where(Message.created_at >= since_30d)
        )
    ).one()

    prompt_tokens = int(tokens[0] or 0)
    completion_tokens = int(tokens[1] or 0)

    return AiHealth(
        # Flipped on when app/api/v1/routes/tutor.py exists and is mounted.
        # Stated rather than guessed: a console that implies a working tutor
        # sends support chasing a model that was never wired up.
        tutor_available=False,
        tutor_status=(
            "Not served yet. /tutor/ask needs the extraction pipeline first — "
            "an answer that cites a page needs the page to have been read."
        ),
        extractable=extractable,
        extracted=extracted,
        pending=pending,
        failed=failed,
        stalled=stalled,
        coverage_pct=round(extracted / extractable * 100, 1) if extractable else 0.0,
        chunks=chunks,
        chunks_per_material=round(chunks / extracted, 1) if extracted else 0.0,
        answers_30d=answers,
        prompt_tokens_30d=prompt_tokens,
        completion_tokens_30d=completion_tokens,
        avg_tokens_per_answer=(
            int((prompt_tokens + completion_tokens) / answers) if answers else 0
        ),
    )


# --- One student, in full ----------------------------------------------------


async def user_activity(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """
    What one student has in the system.

    Nine counts in nine queries rather than one clever join. Each is an indexed
    count over a single user's rows, which is milliseconds; the join that
    returned all nine at once would be unreadable and would start
    double-counting the moment anyone added a tenth.
    """
    return {
        "units": await _count(
            session,
            _count_of(Unit, Unit.user_id == user_id, Unit.deleted_at.is_(None)),
        ),
        "materials": await _count(
            session,
            _count_of(
                Material, Material.user_id == user_id, Material.deleted_at.is_(None)
            ),
        ),
        "events": await _count(
            session,
            _count_of(Event, Event.user_id == user_id, Event.deleted_at.is_(None)),
        ),
        "class_sessions": await _count(
            session,
            _count_of(
                ClassSession,
                ClassSession.user_id == user_id,
                ClassSession.deleted_at.is_(None),
            ),
        ),
        "chats": await _count(
            session,
            _count_of(Chat, Chat.user_id == user_id, Chat.deleted_at.is_(None)),
        ),
        "messages": await _count(
            session, _count_of(Message, Message.user_id == user_id)
        ),
        "study_days": await _count(
            session, _count_of(StudyDay, StudyDay.user_id == user_id)
        ),
        "devices": await _count(session, _count_of(Device, Device.user_id == user_id)),
        "storage_bytes": int(
            await session.scalar(
                select(func.coalesce(func.sum(Material.byte_size), 0)).where(
                    Material.user_id == user_id, Material.deleted_at.is_(None)
                )
            )
            or 0
        ),
    }


async def usage_rows(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[str, dict[str, int]]:
    """Every counter for one student, grouped by metric then period."""
    rows = (
        await session.scalars(
            select(UsageCounter)
            .where(UsageCounter.user_id == user_id)
            .order_by(UsageCounter.metric, UsageCounter.period_key.desc())
        )
    ).all()

    grouped: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        grouped[row.metric][row.period_key] = row.count

    return dict(grouped)


async def group_membership(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[PlanGroup, bool]]:
    """
    The Friends plans this student is on, and whether they own each.

    Owned groups are unioned in rather than assumed: the owner holds a seat
    today, but a support action that frees seats must not make the owner's own
    plan disappear from their profile.
    """
    joined = (
        await session.scalars(
            select(PlanGroup)
            .join(PlanGroupMember, PlanGroupMember.group_id == PlanGroup.id)
            .where(PlanGroupMember.user_id == user_id)
        )
    ).all()

    owned = (
        await session.scalars(select(PlanGroup).where(PlanGroup.owner_id == user_id))
    ).all()

    seen: dict[uuid.UUID, PlanGroup] = {group.id: group for group in joined}
    for group in owned:
        seen.setdefault(group.id, group)

    return [(group, group.owner_id == user_id) for group in seen.values()]


def tier_label(tier: str) -> str:
    return plan_for(tier).name
