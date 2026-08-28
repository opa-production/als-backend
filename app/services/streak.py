from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import StudyDay

#: How far back a streak is computed. A run longer than this is not worth the
#: rows to prove — and the screen only ever draws one week.
LOOKBACK_DAYS = 400


@dataclass
class Streak:
    current: int = 0
    longest: int = 0
    last_day: date | None = None
    #: The seven days of the current week that were studied, Monday first.
    this_week: list[date] = field(default_factory=list)
    total_days: int = 0


async def record_day(
    session: AsyncSession, *, user_id: uuid.UUID, day: date
) -> bool:
    """
    Marks a day as studied. Returns whether it was new.

    Idempotent by constraint, so a client can post on every question without
    counting first — asking three things in an evening is one day of studying,
    not three.
    """
    existing = await session.scalar(
        select(StudyDay).where(StudyDay.user_id == user_id, StudyDay.day == day)
    )
    if existing is not None:
        return False

    session.add(StudyDay(user_id=user_id, day=day))
    await session.flush()
    return True


async def compute(
    session: AsyncSession, *, user_id: uuid.UUID, today: date
) -> Streak:
    """
    Derives the streak from the days themselves.

    Recomputed rather than incremented. A stored counter drifts the first time
    a write is lost or a day is backfilled, and then there is no way to tell
    which number is right — here the days *are* the record, so the count can
    never disagree with them.

    Today not being present does not break the streak: a student who has not
    revised *yet* today still has yesterday's run intact. Only a gap before
    yesterday ends it.
    """
    rows = (
        await session.scalars(
            select(StudyDay.day)
            .where(
                StudyDay.user_id == user_id,
                StudyDay.day > today - timedelta(days=LOOKBACK_DAYS),
            )
            .order_by(StudyDay.day.desc())
        )
    ).all()

    if not rows:
        return Streak()

    days = sorted(set(rows), reverse=True)
    streak = Streak(last_day=days[0], total_days=len(days))

    # --- Current run --------------------------------------------------------
    #
    # Counted back from the newest day rather than from an anchor pinned to
    # `today`. The anchor version asked whether `days[0]` was exactly today or
    # exactly yesterday and abandoned the whole count otherwise, which meant a
    # day *ahead* of `today` scored zero rather than one: `GET /me/streak`
    # dates itself in UTC while `POST /me/streak` stores the student's local
    # day, so for anyone east of UTC every day between midnight and their
    # offset has a newest day the read side thinks is in the future. A live
    # streak read as 0 for those hours, then healed itself later in the day.
    #
    # A run is alive if it reaches yesterday. Today being absent does not end
    # it: the day is not over.
    if days[0] >= today - timedelta(days=1):
        expected = days[0]
        for day in days:
            if day == expected:
                streak.current += 1
                expected -= timedelta(days=1)
            elif day < expected:
                break

    # --- Longest run ever ---------------------------------------------------
    run = 1
    longest = 1
    ordered = sorted(days)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        run = run + 1 if current - previous == timedelta(days=1) else 1
        longest = max(longest, run)

    streak.longest = max(longest, streak.current)

    # --- This week, Monday first -------------------------------------------
    monday = today - timedelta(days=today.weekday())
    week = {monday + timedelta(days=offset) for offset in range(7)}
    streak.this_week = sorted(week & set(days))

    return streak
