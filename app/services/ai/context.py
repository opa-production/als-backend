"""
What the tutor can see about the student it is talking to.

Retrieval answers "what does their material say about X". This answers the
prior question the app kept getting wrong: *what material is there, and which
unit are we in*. Without it the tutor is talking to a stranger — asked which
unit is open it says it cannot see the screen, and asked what a PDF is about it
says it cannot open files, both of which read as the app being broken when the
unit is right there in the composer.

It is deliberately metadata only. Titles, kinds, page counts and whether the
text was readable; never chunk text. The passages are retrieval's job, and
pouring a document into every prompt would cost more than the whole answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Unit
from app.models.knowledge import Material

log = structlog.get_logger()

#: Enough to describe a unit's shelf without turning the system prompt into a
#: file listing. A student with more than this has a bigger problem than the
#: tutor forgetting the twentieth title.
_MAX_MATERIALS = 20

#: Units named when nothing is selected, most recently touched first.
_MAX_UNITS = 12


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
class StudentContext:
    """The situation the question is being asked in."""

    unit_code: str | None = None
    unit_title: str = ""
    materials: list[MaterialCard] = field(default_factory=list)
    #: Every unit the student has, for when none is selected.
    other_units: list[str] = field(default_factory=list)

    @property
    def has_material(self) -> bool:
        return any(card.readable for card in self.materials)


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
        return EMPTY

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

    if selected is None:
        return StudentContext(other_units=[f"{row[1]} — {row[2]}" for row in units])

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
    )
