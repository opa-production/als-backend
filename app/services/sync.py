from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.course import ClassSession, Unit
from app.models.knowledge import Material
from app.models.planner import Event
from app.models.tutor import Chat, Message
from app.schemas.sync import SyncPull, SyncPush, SyncPushResult, TableResult
from app.services.plans import unit_cap
from app.services.quota import Entitlement

#: Rows per table per pull. A student returning after a semester away should
#: get their data in several quick pages rather than one request that times out
#: on a matatu.
PAGE_SIZE = 500


class Conflict:
    """
    Last write wins, decided on ``updated_at``.

    Worth stating plainly because the alternatives were considered and are
    worse here:

    * **Server always wins** loses work a student did offline, which is the
      one thing this app promised not to do.
    * **Client always wins** lets a stale phone that has been in a drawer for a
      month overwrite everything done since on another device.
    * **Merge** needs field-level timestamps and a rule per field. For notes
      and deadlines edited by one person on one or two devices, that is a great
      deal of machinery to resolve a conflict that is rare and low-stakes.

    Last-write-wins is honest about what it does, and because ids are minted by
    the client, replaying the same push changes nothing.
    """


def _is_newer(incoming: datetime, existing: datetime | None) -> bool:
    """
    Strictly newer, so a replay of the same row is a no-op.

    ``>=`` here would make every retry a write, and a device that syncs on a
    loop would rewrite its whole dataset every time.
    """
    current = as_utc(existing)
    return current is None or as_utc(incoming) > current


async def _apply(
    session: AsyncSession,
    *,
    model: type,
    rows: list[Any],
    user_id: uuid.UUID,
    fields: tuple[str, ...],
    result: TableResult,
    guard=None,
) -> None:
    """
    Upserts a batch, honouring last-write-wins.

    Existing rows are fetched in one query and matched in Python rather than
    with ``ON CONFLICT``. The upsert syntax differs between Postgres and
    SQLite, and two dialect-specific branches to save one round trip on a batch
    of a few dozen rows is a bad trade — this path runs a handful of times per
    student per day, not per request.
    """
    if not rows:
        return

    incoming_ids = [row.id for row in rows]
    existing = {
        record.id: record
        for record in (
            await session.scalars(
                select(model).where(model.id.in_(incoming_ids), model.user_id == user_id)
            )
        ).all()
    }

    for row in rows:
        record = existing.get(row.id)

        if record is not None and not _is_newer(row.updated_at, record.updated_at):
            result.skipped += 1
            continue

        if record is None:
            if guard is not None:
                reason = await guard(row)
                if reason is not None:
                    result.rejected.append(f"{row.id}: {reason}")
                    continue

            record = model(id=row.id, user_id=user_id)
            session.add(record)

        for field in fields:
            setattr(record, field, getattr(row, field))

        record.deleted_at = row.deleted_at
        # `updated_at` is taken from the device, not stamped here: the cursor a
        # client pages on has to move forward monotonically, and a row written
        # with the server's clock would come back on the very next pull as if
        # it had changed again.
        record.updated_at = as_utc(row.updated_at)
        result.applied += 1

    await session.flush()


async def push(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    payload: SyncPush,
    #: Not read any more — the unit ceiling is the same on every tier. Kept on
    #: the signature because this is where a per-plan sync limit would go if
    #: one ever returns, and because every caller already resolves it.
    entitlement: Entitlement,  # noqa: ARG001
) -> SyncPushResult:
    """
    Writes everything a device sends, and reports what happened to each table.

    Nothing here fails the whole request because one row was refused. A student
    over their unit cap still needs the rest of their notes to sync — an
    all-or-nothing push would strand every table behind one rejected row.
    """
    result = SyncPushResult(cursor=utc_now())

    # --- Units, capped for everyone ----------------------------------------
    #
    # The same ceiling on every tier, and not a thing that is sold. See
    # `UNIT_HARD_CAP` for why: units cost nothing to hold, so rationing them by
    # plan gated the cheap thing while the expensive ones — pages and questions
    # — are metered on their own.
    cap = unit_cap()
    live_units = await session.scalar(
        select(Unit.id)
        .where(Unit.user_id == user_id, Unit.deleted_at.is_(None))
        .limit(1)
    )
    existing_count = 0
    if live_units is not None:
        existing_count = len(
            (
                await session.scalars(
                    select(Unit.id).where(
                        Unit.user_id == user_id, Unit.deleted_at.is_(None)
                    )
                )
            ).all()
        )

    budget = {"left": max(0, cap - existing_count)}

    async def unit_guard(row):
        # Only new, undeleted units count against the cap. Editing one you
        # already have must never be refused.
        if row.deleted_at is not None:
            return None
        if budget["left"] <= 0:
            # Not "for this plan" any more — there is no plan that lifts it,
            # and a message implying otherwise sends a student to the paywall
            # to buy something that does not exist.
            return f"over the {cap}-unit limit"
        budget["left"] -= 1
        return None

    await _apply(
        session,
        model=Unit,
        rows=payload.units,
        user_id=user_id,
        fields=("code", "title", "lecturer"),
        result=result.units,
        guard=unit_guard,
    )

    await _apply(
        session,
        model=ClassSession,
        rows=payload.class_sessions,
        user_id=user_id,
        fields=("unit_id", "weekday", "starts_at", "ends_at", "room"),
        result=result.class_sessions,
    )

    await _apply(
        session,
        model=Material,
        rows=payload.materials,
        user_id=user_id,
        # Storage columns are deliberately absent: they are set by the upload
        # flow, and letting a client write them would let it claim any path in
        # the bucket, including another student's.
        fields=("unit_id", "kind", "title", "body", "archived"),
        result=result.materials,
    )

    await _apply(
        session,
        model=Event,
        rows=payload.events,
        user_id=user_id,
        fields=("unit_id", "title", "kind", "label", "due_at", "done"),
        result=result.events,
    )

    await _push_chats(session, user_id=user_id, payload=payload, result=result)

    result.cursor = utc_now()
    return result


async def _push_chats(
    session: AsyncSession, *, user_id: uuid.UUID, payload: SyncPush, result
) -> None:
    """
    Chats, with their messages.

    Messages are append-only — a turn that has been said is never edited — so
    they are inserted only when the id is new. That makes replaying a push
    free instead of duplicating a conversation.
    """
    if not payload.chats:
        return

    await _apply(
        session,
        model=Chat,
        rows=payload.chats,
        user_id=user_id,
        fields=("unit_id", "title"),
        result=result.chats,
    )

    incoming_message_ids = [
        message.id for chat in payload.chats for message in chat.messages
    ]
    if not incoming_message_ids:
        return

    known = set(
        (
            await session.scalars(
                select(Message.id).where(Message.id.in_(incoming_message_ids))
            )
        ).all()
    )

    for chat in payload.chats:
        for message in chat.messages:
            if message.id in known:
                continue
            session.add(
                Message(
                    id=message.id,
                    chat_id=chat.id,
                    user_id=user_id,
                    role=message.role,
                    content=message.content,
                    sources=message.sources,
                    created_at=as_utc(message.created_at),
                )
            )

    await session.flush()


async def pull(
    session: AsyncSession, *, user_id: uuid.UUID, since: datetime | None
) -> SyncPull:
    """
    Everything changed after ``since``, tombstones included.

    ``since`` is exclusive. A device passes back the cursor it was given, and
    because writes carry the device's own ``updated_at`` the cursor moves
    forward without the server's clock skewing it.
    """
    out = SyncPull(cursor=utc_now())
    cutoff = as_utc(since)

    async def fetch(model, *, options=None):
        statement = select(model).where(model.user_id == user_id)
        if cutoff is not None:
            statement = statement.where(model.updated_at > cutoff)
        statement = statement.order_by(model.updated_at).limit(PAGE_SIZE)
        if options:
            statement = statement.options(*options)
        return (await session.scalars(statement)).all()

    out.units = list(await fetch(Unit))
    out.class_sessions = list(await fetch(ClassSession))
    out.materials = list(await fetch(Material))
    out.events = list(await fetch(Event))

    chats = await fetch(Chat, options=[selectinload(Chat.messages)])
    out.chats = list(chats)

    # A full page almost certainly means there is more behind it. Saying so is
    # cheaper than a count, and the client simply pulls again from the last
    # row's timestamp.
    out.has_more = any(
        len(rows) >= PAGE_SIZE
        for rows in (out.units, out.class_sessions, out.materials, out.events, chats)
    )

    if out.has_more:
        # Do not advance past what was actually sent, or the unsent tail is
        # skipped forever.
        newest = [
            as_utc(row.updated_at)
            for rows in (out.units, out.class_sessions, out.materials, out.events, chats)
            for row in rows[-1:]
        ]
        if newest:
            out.cursor = min(newest)

    return out
