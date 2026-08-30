"""
What the tutor can see about the student it is talking to.

Retrieval answers "what does their material say about X". This answers the
prior question the app kept getting wrong: *who is this, what have they got,
and what is coming at them this week*. Without it the tutor is talking to a
stranger — asked which unit is open it says it cannot see the screen, and asked
what a PDF is about it says it cannot open files, both of which read as the app
being broken when the unit is right there in the composer.

Four things go in, and each earns its tokens by removing a denial the tutor
would otherwise make:

* **Who they are.** A first name, their year and programme. It is the
  difference between an assistant and a search box, and it costs a dozen
  tokens.
* **The unit they have open, and every unit they have.**
* **What is filed under it** — titles, kinds, page counts and whether the text
  came out readable.
* **Their week.** Today's and tomorrow's classes, and the deadlines closing in.
  This is the part a general chatbot structurally cannot have, and it is what
  makes "help me revise" answerable with "your CAT is on Friday, so start with
  the two topics it always covers".
* **How long they have kept it up.** The revision streak the app already draws
  on its own screen, so the tutor is not the one part of the product that has
  no idea the student has shown up eleven days running.

It is deliberately metadata only. Never chunk text — the passages are
retrieval's job, and pouring a document into every prompt would cost more than
the whole answer. And never contact details: a phone number in a prompt is a
phone number in a third party's logs, and it answers nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, time, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.course import ClassSession, Unit
from app.models.knowledge import Material
from app.models.planner import Event
from app.services import streak as streak_service
from app.services.zones import user_zone

log = structlog.get_logger()

#: Enough to describe a unit's shelf without turning the system prompt into a
#: file listing. A student with more than this has a bigger problem than the
#: tutor forgetting the twentieth title.
_MAX_MATERIALS = 20

#: Units named when nothing is selected, most recently touched first.
_MAX_UNITS = 12

#: Deadlines named, soonest first. Five is a week or two of real life; beyond
#: that it stops being context and starts being a calendar dump the model has
#: to wade through to answer a question about enzymes.
_MAX_DEADLINES = 5

#: Below this a "streak" is just today, and saying so is noise.
_STREAK_WORTH_MENTIONING = 2

#: How far out a deadline is worth mentioning. Something due in March is not
#: what a student needs prompting about in a chat window in August.
_DEADLINE_HORIZON = timedelta(days=30)


@dataclass(frozen=True)
class MaterialCard:
    """One filed item, as the tutor should describe it."""

    title: str
    kind: str
    page_count: int | None
    extraction_status: str

    @property
    def readable(self) -> bool:
        """
        Whether there is any text the tutor could be given passages from.

        A note is its own text and needs no worker. Anything uploaded is only
        readable once extraction has finished — and saying so plainly is much
        better than the tutor implying the file does not exist.
        """
        return self.kind == "note" or self.extraction_status == "done"

    def describe(self) -> str:
        pages = f", {self.page_count} pages" if self.page_count else ""
        line = f'"{self.title}" ({self.kind}{pages})'

        if self.readable:
            return line
        if self.extraction_status == "failed":
            return f"{line} — its text could not be read, so it cannot be quoted"
        return f"{line} — still being processed, so it cannot be quoted yet"


@dataclass(frozen=True)
class ClassSlot:
    """One lecture on the timetable, as it should be read out."""

    unit_code: str
    starts_at: time
    room: str

    def describe(self) -> str:
        where = f" in {self.room}" if self.room else ""
        return f"{self.unit_code} at {self.starts_at.strftime('%H:%M')}{where}"


def _kind_word(kind: str) -> str:
    return "CAT" if kind.lower() == "cat" else kind.capitalize()


@dataclass(frozen=True)
class Deadline:
    """Something with a date, and how far off it is."""

    title: str
    kind: str
    unit_code: str
    due_on: date
    days_away: int

    def describe(self) -> str:
        when = (
            "today"
            if self.days_away == 0
            else "tomorrow"
            if self.days_away == 1
            else f"in {self.days_away} days"
        )
        unit = f" ({self.unit_code})" if self.unit_code else ""
        # The kind is worth saying: a CAT in four days and an essay in four
        # days are not the same amount of trouble. "CAT" is an initialism and
        # has to read as one, or the tutor says "cat" back to the student.
        kind = "" if self.kind in ("other", "") else f"{_kind_word(self.kind)}: "
        # Built rather than one format string: "%-d" drops the leading zero on
        # Linux and raises on Windows, where the tests also run.
        stamp = f"{self.due_on:%a} {self.due_on.day} {self.due_on:%b}"
        return f"{kind}{self.title}{unit}, {stamp} ({when})"


@dataclass(frozen=True)
class StudentContext:
    """The situation the question is being asked in."""

    unit_code: str | None = None
    unit_title: str = ""
    materials: list[MaterialCard] = field(default_factory=list)
    #: Every unit the student has, for when none is selected.
    other_units: list[str] = field(default_factory=list)

    # --- Who is asking ----------------------------------------------------
    #: First name only. The tutor addresses a person, not a full legal name,
    #: and a surname in a prompt buys nothing the given name does not.
    first_name: str = ""
    programme: str = ""
    institution: str = ""
    year_of_study: int | None = None

    # --- Their week -------------------------------------------------------
    classes_today: list[ClassSlot] = field(default_factory=list)
    classes_tomorrow: list[ClassSlot] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)
    #: Consecutive days revised, up to and including yesterday. A run that has
    #: not been continued today is still a live run — the day is not over.
    streak_days: int = 0
    #: Whether today is already one of them. The difference between "you are on
    #: eleven days" and "keep it going today" is this flag, and getting it
    #: backwards congratulates someone for a day they have not had yet.
    studied_today: bool = False
    #: The student's own weekday name, so "today" in the prompt is today where
    #: they are rather than wherever the server happens to be.
    today_name: str = ""

    @property
    def has_material(self) -> bool:
        return any(card.readable for card in self.materials)

    @property
    def knows_the_student(self) -> bool:
        return bool(self.first_name or self.programme or self.institution)

    @property
    def has_a_week(self) -> bool:
        return bool(self.classes_today or self.classes_tomorrow or self.deadlines)

    @property
    def has_a_streak(self) -> bool:
        """
        Whether the run is worth a line in the prompt.

        Two days is where a streak starts being a thing a person is doing
        rather than a thing that happened. One day is just "they used the app",
        which the model can see from the fact that it is being asked a
        question.
        """
        return self.streak_days >= _STREAK_WORTH_MENTIONING


EMPTY = StudentContext()


async def load(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unit_code: str | None,
) -> StudentContext:
    """
    The selected unit and what is filed under it.

    Failure here degrades to `EMPTY`, which is exactly the tutor's behaviour
    before this module existed. A question should never fail because the
    sidebar could not be described.
    """
    try:
        return await _load(session, user_id, unit_code)
    except SQLAlchemyError:
        log.exception("tutor_context_failed", user_id=str(user_id))
        return EMPTY


async def _load(
    session: AsyncSession, user_id: uuid.UUID, unit_code: str | None
) -> StudentContext:
    units = (
        await session.execute(
            select(Unit.id, Unit.code, Unit.title)
            .where(Unit.user_id == user_id, Unit.deleted_at.is_(None))
            .order_by(Unit.updated_at.desc())
            .limit(_MAX_UNITS)
        )
    ).all()

    if not units:
        # No units is not the same as knowing nothing. A student who has just
        # signed up still has a name, and being greeted by it is most of what
        # "this app knows me" means on the first screen they see.
        return StudentContext(**await _student(session, user_id))

    selected = None
    if unit_code:
        wanted = unit_code.strip().upper()
        selected = next((row for row in units if row[1].upper() == wanted), None)

        if selected is None:
            # Selected but outside the recent window, or soft-deleted since.
            # Worth one more query: naming the wrong unit is worse than naming
            # none, and this is the unit the student is looking at.
            selected = (
                await session.execute(
                    select(Unit.id, Unit.code, Unit.title).where(
                        Unit.user_id == user_id,
                        Unit.deleted_at.is_(None),
                        func.upper(Unit.code) == wanted,
                    )
                )
            ).first()

    personal = await _student(session, user_id)

    if selected is None:
        return StudentContext(
            other_units=[f"{row[1]} — {row[2]}" for row in units], **personal
        )

    rows = (
        await session.execute(
            select(
                Material.title,
                Material.kind,
                Material.page_count,
                Material.extraction_status,
            )
            .where(
                Material.user_id == user_id,
                Material.unit_id == selected[0],
                Material.deleted_at.is_(None),
                Material.archived.is_(False),
            )
            .order_by(Material.created_at.desc())
            .limit(_MAX_MATERIALS)
        )
    ).all()

    return StudentContext(
        unit_code=selected[1],
        unit_title=selected[2],
        materials=[
            MaterialCard(
                title=row[0], kind=row[1], page_count=row[2], extraction_status=row[3]
            )
            for row in rows
        ],
        other_units=[f"{row[1]} — {row[2]}" for row in units],
        **personal,
    )


async def _student(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """
    Who the student is, and what their week looks like.

    Returned as keyword arguments rather than a second object, so the three
    places that build a ``StudentContext`` cannot each forget a different half
    of it.

    ``session.get`` rather than a select: the account was already loaded by the
    auth dependency on this same session, so the identity map answers it
    without going back to the database.
    """
    account = await session.get(User, user_id)
    zone = await user_zone(session, user_id)
    now_local = utc_now().astimezone(zone)
    today = now_local.date()

    classes = await _classes(session, user_id, today)
    run = await streak_service.compute(session, user_id=user_id, today=today)

    return {
        # First name only. A surname buys the tutor nothing, and "Hi Brian" is
        # the whole effect.
        "first_name": (account.full_name or "").strip().split(" ")[0]
        if account
        else "",
        "programme": account.program if account else "",
        "institution": account.institution if account else "",
        "year_of_study": account.year_of_study if account else None,
        "today_name": today.strftime("%A"),
        "classes_today": classes[0],
        "classes_tomorrow": classes[1],
        "deadlines": await _deadlines(session, user_id, zone, now_local),
        "streak_days": run.current,
        "studied_today": run.last_day == today,
    }


async def _classes(
    session: AsyncSession, user_id: uuid.UUID, today: date
) -> tuple[list[ClassSlot], list[ClassSlot]]:
    """
    Today's and tomorrow's timetable, in the order they happen.

    Two days rather than the whole week: a student asking a question at eleven
    on a Tuesday is not helped by Friday's lecture, and every line here is paid
    for on every question they ask.
    """
    tomorrow = today + timedelta(days=1)
    wanted = {
        ClassSession.weekday_of(today): [],
        ClassSession.weekday_of(tomorrow): [],
    }

    rows = (
        await session.execute(
            select(ClassSession.weekday, ClassSession.starts_at, ClassSession.room, Unit.code)
            .join(Unit, Unit.id == ClassSession.unit_id)
            .where(
                ClassSession.user_id == user_id,
                ClassSession.deleted_at.is_(None),
                ClassSession.weekday.in_(wanted),
                Unit.deleted_at.is_(None),
            )
            .order_by(ClassSession.starts_at)
        )
    ).all()

    for weekday, starts_at, room, code in rows:
        wanted[weekday].append(
            ClassSlot(unit_code=code, starts_at=starts_at, room=room)
        )

    return (
        wanted[ClassSession.weekday_of(today)],
        wanted[ClassSession.weekday_of(tomorrow)],
    )


async def _deadlines(
    session: AsyncSession,
    user_id: uuid.UUID,
    zone,
    now_local,
) -> list[Deadline]:
    """
    What is closing in, soonest first.

    Only what is unfinished, dated, still ahead and inside the horizon —
    everything else is either done or too far off to be the reason a student
    opened the app tonight.

    Dates are read in the student's zone. ``due_at`` is an instant the client
    set to 23:59 local, so converting anywhere else moves "due Friday" onto
    Thursday for everyone east of UTC, which is everyone here.
    """
    now = utc_now()
    rows = (
        await session.execute(
            select(Event.title, Event.kind, Event.label, Event.due_at, Unit.code)
            .outerjoin(Unit, Unit.id == Event.unit_id)
            .where(
                Event.user_id == user_id,
                Event.deleted_at.is_(None),
                Event.done.is_(False),
                Event.due_at.is_not(None),
                Event.due_at >= now,
                Event.due_at <= now + _DEADLINE_HORIZON,
            )
            .order_by(Event.due_at)
            .limit(_MAX_DEADLINES)
        )
    ).all()

    deadlines = []
    for title, kind, label, due_at, code in rows:
        due_on = as_utc(due_at).astimezone(zone).date()
        deadlines.append(
            Deadline(
                title=title,
                # "other" carries the student's own word for it, which is a
                # better label than the fallback the fixed list gave it.
                kind=label if kind == "other" and label else kind,
                unit_code=code or "",
                due_on=due_on,
                days_away=(due_on - now_local.date()).days,
            )
        )
    return deadlines
