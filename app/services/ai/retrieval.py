"""
Finding the passages an answer should be built from.

This is what decides whether the tutor says "here is what your notes say" or "I
could not find this in your material". That makes the *score* as important as
the passages: a confident answer built from three irrelevant paragraphs is worse
than an honest one built from none.

Postgres full-text search rather than embeddings, for now. `material_chunks` is
laid out for a pgvector column and this module is the seam where it goes, but
keyword search over a student's own twenty documents is a much better baseline
than it would be over the open web — the vocabulary is small, technical and
shared between the question and the source.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import Float, cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.course import Unit
from app.models.knowledge import Material, MaterialChunk

log = structlog.get_logger()


@dataclass(frozen=True)
class Passage:
    """One retrieved chunk, and enough to cite it."""

    material_id: uuid.UUID
    title: str
    unit_code: str
    page_number: int | None
    content: str
    score: float

    def citation(self) -> str:
        """How this passage is referred to in the prompt and in the answer."""
        where = f", page {self.page_number}" if self.page_number else ""
        if self.unit_code:
            return f"{self.unit_code} — {self.title}{where}"
        return f"{self.title}{where}"


#: Words too common to narrow anything down, and common enough in a question
#: that leaving them in drags in unrelated passages. Deliberately short: an
#: aggressive stop list throws away the technical terms that do the work.
_NOISE = {
    "what", "which", "when", "where", "who", "why", "how", "does", "did", "is",
    "are", "was", "were", "the", "a", "an", "and", "or", "but", "of", "in", "on",
    "for", "to", "from", "with", "about", "explain", "describe", "tell", "me",
    "please", "can", "you", "i", "my", "this", "that", "it", "its", "be", "been",
}

_WORD = re.compile(r"[A-Za-z0-9_]+")


def keywords(question: str) -> list[str]:
    """The words worth searching for."""
    words = [word.lower() for word in _WORD.findall(question)]
    return [word for word in words if len(word) > 2 and word not in _NOISE]


async def search(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    question: str,
    unit_code: str | None = None,
    limit: int | None = None,
) -> list[Passage]:
    """
    The student's own material, ranked against the question.

    Scoped to one user without exception. `material_chunks.user_id` is
    denormalised from the material precisely so this filter needs no join and
    can never be forgotten — a retrieval bug that leaked one student's notes
    into another's answer would be the worst failure this system could have.
    """
    terms = keywords(question)
    if not terms:
        return []

    top_k = limit or settings.ai_retrieval_top_k
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"

    if dialect != "postgresql":
        return await _search_portable(session, user_id, terms, unit_code, top_k)

    # Retrieval failing must not fail the answer.
    #
    # A search that raises leaves the transaction aborted on Postgres, and
    # every later statement in the request dies with InFailedSQLTransactionError
    # naming an innocent query -- which is how a broken full-text query
    # presented as the quota counter being broken. The savepoint contains it,
    # and an empty result is a meaningful answer here: the tutor says it found
    # nothing in the material and answers generally, which is exactly the
    # behaviour a student with no uploads already gets.
    try:
        async with session.begin_nested():
            return await _search_postgres(session, user_id, terms, unit_code, top_k)
    except SQLAlchemyError:
        log.exception("retrieval_failed", user_id=str(user_id))
        return []


def _base_query(user_id: uuid.UUID, unit_code: str | None):
    """
    Chunks joined to the material and unit they came from.

    Both joins earn their place: the title and the unit code are what a citation
    is made of, and a student asking inside one unit should not be answered from
    another.
    """
    query = (
        select(
            MaterialChunk.material_id,
            Material.title,
            Unit.code,
            MaterialChunk.page_number,
            MaterialChunk.content,
        )
        .join(Material, Material.id == MaterialChunk.material_id)
        .join(Unit, Unit.id == Material.unit_id)
        .where(
            MaterialChunk.user_id == user_id,
            Material.deleted_at.is_(None),
            Unit.deleted_at.is_(None),
        )
    )

    if unit_code:
        query = query.where(func.upper(Unit.code) == unit_code.strip().upper())

    return query


async def _search_postgres(
    session: AsyncSession,
    user_id: uuid.UUID,
    terms: list[str],
    unit_code: str | None,
    top_k: int,
) -> list[Passage]:
    """
    `ts_rank` over the chunk text.

    `plainto_tsquery` ANDs its terms, which is too strict for a question — one
    unmatched word and a relevant passage scores nothing. The terms are ORed
    explicitly so ranking, not matching, does the discriminating.
    """
    query_text = " | ".join(terms)

    # The configuration must be typed as regconfig. Sent as a plain bind
    # parameter it arrives with no type, and Postgres cannot choose between
    # to_tsvector(regconfig, text) and to_tsvector(text) -- it rejects the
    # statement rather than guessing. psycopg2 inlines literals so it never
    # hit this; asyncpg uses real prepared statements, so it always does.
    #
    # This path runs only on Postgres, and the tests run on SQLite, so nothing
    # in CI exercises it. Whatever it raised aborted the request's transaction,
    # and the traceback that surfaced named the next query to touch the
    # session -- record_usage -- rather than this one.
    config = literal("english", type_=REGCONFIG)
    vector = func.to_tsvector(config, MaterialChunk.content)
    tsquery = func.to_tsquery(config, literal(query_text))
    rank = cast(func.ts_rank(vector, tsquery), Float)

    statement = (
        _base_query(user_id, unit_code)
        .add_columns(rank.label("score"))
        .where(vector.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(top_k)
    )

    rows = (await session.execute(statement)).all()
    return [
        Passage(
            material_id=row[0],
            title=row[1],
            unit_code=row[2],
            page_number=row[3],
            content=row[4],
            score=float(row[5] or 0.0),
        )
        for row in rows
    ]


async def _search_portable(
    session: AsyncSession,
    user_id: uuid.UUID,
    terms: list[str],
    unit_code: str | None,
    top_k: int,
) -> list[Passage]:
    """
    The same search where there is no full-text index — SQLite, and so the
    tests.

    Candidates are narrowed in SQL by a LIKE on any term, then scored in Python.
    The scoring deliberately mirrors what `ts_rank` rewards — how many distinct
    query terms appear, and how often — so a threshold tuned on one engine means
    roughly the same thing on the other. It is not identical and does not need
    to be: what the tests check is the *decision* this drives, not the ordering.
    """
    from sqlalchemy import or_

    lowered = func.lower(MaterialChunk.content)
    statement = (
        _base_query(user_id, unit_code)
        .where(or_(*[lowered.like(f"%{term}%") for term in terms]))
        .limit(top_k * 8)
    )

    rows = (await session.execute(statement)).all()
    scored: list[Passage] = []

    for row in rows:
        content = row[4] or ""
        haystack = content.lower()
        hits = sum(1 for term in terms if term in haystack)
        if not hits:
            continue

        occurrences = sum(haystack.count(term) for term in terms)
        coverage = hits / len(terms)
        # Bounded so a very long chunk that repeats one word cannot outrank a
        # short one that matches everything.
        density = min(1.0, occurrences / 12)
        scored.append(
            Passage(
                material_id=row[0],
                title=row[1],
                unit_code=row[2],
                page_number=row[3],
                content=content,
                score=round(coverage * 0.75 + density * 0.25, 4),
            )
        )

    scored.sort(key=lambda passage: passage.score, reverse=True)
    return scored[:top_k]


def is_grounded(passages: list[Passage]) -> bool:
    """
    Whether the student's own material actually answers this.

    The top score alone, not an average: one strongly matching passage is a
    real answer, and averaging it with five weak ones would throw that away.
    """
    if not passages:
        return False
    return passages[0].score >= settings.ai_retrieval_min_score
