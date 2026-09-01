"""
Deciding what to notify a student about, and when.

The whole design turns on one thing: *the server does not know what time it is
for the student*. A deadline is stored as an instant, a class as a wall-clock
slot, and quiet hours as a preference in a timezone the phone reported. So each
of the three has to be brought into the same frame before any of them can be
compared, and this module is where that happens — once, in one place, rather
than three subtly different times across a sweep.

Everything here is idempotent by construction. The sweep runs every minute and
a deadline sits inside its lead window for that whole window, so "have I already
said this" cannot be a judgement about timestamps; it is a unique row in
``notification_log``. See that model for why the key is shaped the way it is.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.account import Device
from app.models.course import ClassSession, Unit
from app.models.knowledge import EXTRACTABLE_KINDS, Material
from app.models.notification import NotificationLog
from app.models.planner import Event
from app.models.settings import UserSettings
from app.services import push as push_service
from app.services.push import PushMessage, PushProvider
from app.services.zones import zone_for

log = structlog.get_logger()

UTC = ZoneInfo("UTC")

#: The largest lead any student can set (``reminder_lead_minutes`` is capped at
#: 1440). It bounds the deadline query: nothing further out than this can be due
#: for a nudge on this pass, whatever anyone's preferences say.
MAX_LEAD = timedelta(minutes=1440)

def _default_settings() -> UserSettings:
    """
    What a student who has never opened Settings is treated as having.

    Built by reading the column defaults rather than repeating them, because a
    second copy of "reminders are on, quiet hours are 22:00" would drift from
    the model the first time one of them changed — and drift silently, in the
    direction of notifying people who had opted out.

    An unsaved instance is not enough on its own: SQLAlchemy applies a column
    default at INSERT, so every attribute on a bare ``UserSettings()`` is None.
    """
    row = UserSettings()
    for column in UserSettings.__table__.columns:
        default = column.default
        if default is not None and default.is_scalar:
            setattr(row, column.key, default.arg)
    return row


#: Read once: the columns cannot change at runtime.
DEFAULTS = _default_settings()


@dataclass(slots=True)
class Reminder:
    """One nudge, for one student, before it has been addressed to devices."""

    user_id: uuid.UUID
    kind: str
    dedupe_key: str
    title: str
    body: str
    subject_id: uuid.UUID
    #: The instant the thing being reminded about happens, in UTC.
    scheduled_for: datetime
    #: For a coalesced notification: every material it speaks for, so they can
    #: be stamped as told once it is actually reserved. Empty for reminders
    #: that are about one event or one class.
    material_ids: list[uuid.UUID] = field(default_factory=list)


# --- The sweep ----------------------------------------------------------------


async def sweep(
    session: AsyncSession,
    *,
    client: httpx.AsyncClient,
    now: datetime | None = None,
    provider: PushProvider | None = None,
) -> int:
    """
    One pass. Returns how many notifications were sent.

    Scoped from the devices inward, deliberately: a student with no push token
    can generate no notification, and there are far fewer registered devices
    than there are events. That ordering is what keeps this a small query rather
    than a scan of everyone's planner every minute.
    """
    moment = as_utc(now) or utc_now()
    provider = provider or push_service.get_push_provider(client)

    tokens = await _tokens_by_user(session)
    if not tokens:
        return 0

    preferences = await _settings_by_user(session, tokens)

    due: list[Reminder] = []
    due.extend(await _due_deadlines(session, preferences, moment))
    due.extend(await _due_classes(session, preferences, moment))
    due.extend(await _finished_materials(session, preferences, moment))

    if not due:
        return 0

    # Quiet hours are applied last, and by dropping rather than recording. A
    # reminder silenced at 23:00 has to stay eligible: if the deadline is still
    # ahead when the window lifts, the student should hear about it.
    audible = [
        reminder
        for reminder in due
        if not _in_quiet_hours(preferences[reminder.user_id], moment)
    ]

    reserved = await _reserve(session, audible, moment)
    if not reserved:
        return 0

    # Before delivery, not after: a push that fails to send has still been
    # decided, and retrying it on the next sweep would notify twice as often as
    # the provider is flaky.
    await _mark_notified(session, reserved, moment)

    return await _deliver(session, reserved, tokens, provider)


async def _tokens_by_user(session: AsyncSession) -> dict[uuid.UUID, list[str]]:
    """Every user reachable by a notification, and the tokens to reach them."""
    rows = await session.scalars(
        select(Device).where(Device.push_token.is_not(None))
    )

    tokens: dict[uuid.UUID, list[str]] = {}
    for device in rows:
        if push_service.looks_like_expo_token(device.push_token):
            tokens.setdefault(device.user_id, []).append(device.push_token)

    return tokens


async def _settings_by_user(
    session: AsyncSession, users: Iterable[uuid.UUID]
) -> dict[uuid.UUID, UserSettings]:
    """
    Preferences for those users, defaulted where the row does not exist yet.

    The row is created on first read of ``/me/settings``, so a student who has
    allowed notifications but never opened the screen has none. Falling back to
    the defaults is the behaviour the settings screen itself describes.
    """
    ids = list(users)
    rows = await session.scalars(
        select(UserSettings).where(UserSettings.user_id.in_(ids))
    )

    found = {row.user_id: row for row in rows}
    return {user_id: found.get(user_id, DEFAULTS) for user_id in ids}


# --- Deadlines ----------------------------------------------------------------


async def _due_deadlines(
    session: AsyncSession,
    preferences: dict[uuid.UUID, UserSettings],
    moment: datetime,
) -> list[Reminder]:
    """
    Events entering their lead window.

    The bound is the *largest* lead anyone can set; each row is then held
    against its own owner's. Doing the arithmetic per user in SQL would mean a
    correlated subquery against a settings row that may not exist, for a saving
    of a few dozen rows.
    """
    rows = await session.scalars(
        select(Event).where(
            Event.user_id.in_(preferences.keys()),
            Event.done.is_(False),
            Event.deleted_at.is_(None),
            Event.due_at.is_not(None),
            Event.due_at > moment,
            Event.due_at <= moment + MAX_LEAD,
        )
    )

    reminders = []
    for event in rows:
        preference = preferences[event.user_id]
        if not preference.deadline_reminders:
            continue

        due_at = as_utc(event.due_at)
        minutes_away = (due_at - moment).total_seconds() / 60
        if minutes_away > preference.reminder_lead_minutes:
            continue

        reminders.append(
            Reminder(
                user_id=event.user_id,
                kind="deadline",
                # The due date is part of the key on purpose: moving a deadline
                # is a different occurrence and earns a fresh nudge, while the
                # same deadline swept sixty times does not.
                dedupe_key=f"event:{event.id}:{due_at.isoformat()}",
                title=f"{_kind_word(event)} due {_in_words(minutes_away)}",
                body=event.title,
                subject_id=event.id,
                scheduled_for=due_at,
            )
        )

    return reminders


def _kind_word(event: Event) -> str:
    """What to call this on the lock screen."""
    if event.kind == "other":
        return (event.label or "Task").strip()[:40]
    return {
        "assignment": "Assignment",
        "cat": "CAT",
        "exam": "Exam",
        "project": "Project",
    }.get(event.kind, "Task")


# --- Classes ------------------------------------------------------------------


async def _due_classes(
    session: AsyncSession,
    preferences: dict[uuid.UUID, UserSettings],
    moment: datetime,
) -> list[Reminder]:
    """
    Timetable slots entering their lead window.

    A slot is a weekday and a wall-clock time, so it has no instant until it is
    placed in the student's own timezone. Both today and tomorrow are checked
    there: a lead of twelve hours set on a Monday evening is asking about
    Tuesday's eight o'clock, and looking only at the local day would miss it
    every time.
    """
    rows = (
        await session.execute(
            select(ClassSession, Unit)
            .join(Unit, Unit.id == ClassSession.unit_id)
            .where(
                ClassSession.user_id.in_(preferences.keys()),
                ClassSession.deleted_at.is_(None),
            )
        )
    ).all()

    reminders = []
    for slot, unit in rows:
        preference = preferences[slot.user_id]
        if not preference.class_reminders:
            continue

        zone = _zone(preference)
        local_now = moment.astimezone(zone)

        for day in (local_now.date(), local_now.date() + timedelta(days=1)):
            # 0 = Sunday on the model, matching JavaScript's getDay(); Python's
            # weekday() is Monday-based, so this is not a straight comparison.
            # `weekday_of` is the model's own conversion.
            if ClassSession.weekday_of(day) != slot.weekday:
                continue

            starts_at = datetime.combine(day, slot.starts_at, tzinfo=zone)
            minutes_away = (starts_at - local_now).total_seconds() / 60

            if not 0 < minutes_away <= preference.reminder_lead_minutes:
                continue

            reminders.append(
                Reminder(
                    user_id=slot.user_id,
                    kind="class",
                    #: The local day, not the instant — the same lecture must be
                    #: one occurrence however the sweep's clock is placed.
                    dedupe_key=f"class:{slot.id}:{day.isoformat()}",
                    title=f"{unit.code} starts {_in_words(minutes_away)}",
                    body=_class_body(slot, unit),
                    subject_id=slot.id,
                    scheduled_for=starts_at.astimezone(UTC),
                )
            )

    return reminders


def _class_body(slot: ClassSession, unit: Unit) -> str:
    when = slot.starts_at.strftime("%H:%M")
    if slot.room:
        return f"{unit.title} · {when} · {slot.room}"
    return f"{unit.title} · {when}"


# --- Quiet hours --------------------------------------------------------------


def _zone(preference: UserSettings) -> ZoneInfo:
    """The student's timezone, resolved by the same rule quotas use."""
    return zone_for(preference.timezone)


def _in_quiet_hours(preference: UserSettings, moment: datetime) -> bool:
    local = moment.astimezone(_zone(preference)).time()

    start = _parse_clock(preference.quiet_hours_start)
    end = _parse_clock(preference.quiet_hours_end)

    if start is None or end is None or start == end:
        # Equal bounds are the only sane reading of "no quiet hours". Treating
        # them as a 24-hour window would silence the app completely, which is
        # never what someone setting both to the same value meant.
        return False

    if start < end:
        return start <= local < end

    # The ordinary case: 22:00 to 06:00 crosses midnight.
    return local >= start or local < end


def _parse_clock(value: str) -> time | None:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (AttributeError, ValueError):
        return None


def _in_words(minutes: float) -> str:
    """How a person would say it, not how a clock would."""
    minutes = round(minutes)
    if minutes <= 1:
        return "in a minute"
    if minutes < 60:
        return f"in {minutes} minutes"

    hours = round(minutes / 60)
    if hours == 1:
        return "in an hour"
    if hours < 24:
        return f"in {hours} hours"

    days = round(hours / 24)
    return "tomorrow" if days == 1 else f"in {days} days"


# --- Reserving and sending ----------------------------------------------------


async def _reserve(
    session: AsyncSession, reminders: Sequence[Reminder], moment: datetime
) -> list[tuple[Reminder, NotificationLog]]:
    """
    Claim each reminder before sending it.

    The row goes in *first*, and the unique constraint on
    ``(user_id, dedupe_key)`` is what decides. Checking for an existing row and
    then inserting would leave a window in which two workers — which is every
    deploy, briefly — both look, both see nothing, and both send.

    Each insert gets its own savepoint so one collision does not roll back the
    reminders alongside it.
    """
    claimed = []

    for reminder in reminders:
        row = NotificationLog(
            user_id=reminder.user_id,
            kind=reminder.kind,
            dedupe_key=reminder.dedupe_key,
            title=reminder.title[:120],
            body=reminder.body[:300],
            status="sent",
            scheduled_for=reminder.scheduled_for,
            sent_at=moment,
        )
        try:
            async with session.begin_nested():
                session.add(row)
        except IntegrityError:
            # Already sent — by an earlier sweep, or by the other worker.
            continue

        claimed.append((reminder, row))

    await session.commit()
    return claimed


async def _deliver(
    session: AsyncSession,
    reserved: list[tuple[Reminder, NotificationLog]],
    tokens: dict[uuid.UUID, list[str]],
    provider: PushProvider,
) -> int:
    """
    Send the claimed reminders, then record what actually happened.

    One notification can address several devices; a student with a phone and a
    tablet should hear it on both. It counts as sent if any of them took it, so
    a dead tablet does not mark the phone's nudge failed.
    """
    messages: list[PushMessage] = []
    #: Which slice of `messages` belongs to which reminder, so the per-message
    #: results can be folded back onto the row that claimed them.
    spans: list[tuple[NotificationLog, int, int]] = []

    for reminder, row in reserved:
        start = len(messages)
        for token in tokens.get(reminder.user_id, []):
            messages.append(
                PushMessage(
                    token=token,
                    title=reminder.title,
                    body=reminder.body,
                    data=push_service.notification_data(
                        reminder.kind, reminder.subject_id
                    ),
                )
            )
        spans.append((row, start, len(messages)))

    if not messages:
        return 0

    result = await provider.send(messages)

    sent = 0
    for row, start, end in spans:
        if any(result.ok[start:end]):
            sent += 1
            continue

        row.status = "failed"
        row.error = next((e for e in result.errors[start:end] if e), "")[:300]
        row.sent_at = None

    await _clear_dead_tokens(session, result.dead_tokens)
    await session.commit()

    log.info("reminder_sweep", sent=sent, considered=len(reserved))
    return sent


async def _clear_dead_tokens(session: AsyncSession, dead: set[str]) -> None:
    """
    Forget tokens Expo says will never work again.

    Without this the same uninstalled app is pushed to on every sweep forever,
    and the log fills with a failure nobody can act on.
    """
    if not dead:
        return

    await session.execute(
        update(Device)
        .where(Device.push_token.in_(dead))
        .values(push_token=None)
    )
    log.info("push_tokens_cleared", count=len(dead))


# --- One-off sends ------------------------------------------------------------


async def send_test(
    session: AsyncSession, *, user_id: uuid.UUID, client: httpx.AsyncClient
) -> int:
    """
    A notification to every device on an account, right now.

    Exists because "is push working" is otherwise unanswerable without waiting
    for a real deadline: permissions, the token, the Expo project and the
    credentials all fail in the same silent way. Quiet hours are ignored — this
    is asked for by the person holding the phone.
    """
    tokens = (await _tokens_by_user(session)).get(user_id, [])
    if not tokens:
        return 0

    provider = push_service.get_push_provider(client)
    result = await provider.send(
        [
            PushMessage(
                token=token,
                title="Notifications are on",
                body="This is what a reminder will look like.",
                data=push_service.notification_data("test", None),
            )
            for token in tokens
        ]
    )

    await _clear_dead_tokens(session, result.dead_tokens)
    await session.flush()
    return result.sent


# --- Documents that finished --------------------------------------------------


#: The statuses that are worth interrupting somebody for, because nothing more
#: is going to happen on its own.
#:
#: `failed` and `skipped` are in here deliberately. They are the ones that
#: currently sit unnoticed for days — they need the student to *do* something,
#: and unlike `done` there is no other moment when they will find out.
TERMINAL = ("done", "failed", "skipped")

#: How recently a material must have finished to be worth a notification.
#:
#: "Your notes are ready" is only true near the moment it becomes true. Without
#: this, deploying the feature would announce every document in the table —
#: "CS201: 12 pages are ready" for coursework a student filed in July — and any
#: gap in the worker would later produce the same thing in miniature.
#:
#: A day, and the number is set by quiet hours rather than by staleness.
#:
#: Six hours was the first guess and it is wrong: the default quiet window is
#: 22:00–06:00, so a student who files notes at half past ten at night has their
#: notification held for seven and a half hours and then silently dropped for
#: being too old. Quiet hours are meant to *delay* a notification, not cancel
#: it, and any window shorter than the longest quiet period turns one into the
#: other.
#:
#: Twenty-four hours clears that with room to spare, and still refuses anything
#: genuinely stale — the case this exists for is a document filed in July, not
#: one filed last night.
FINISHED_WINDOW = timedelta(hours=24)


async def _finished_materials(
    session: AsyncSession,
    preferences: dict[uuid.UUID, UserSettings],
    moment: datetime,
) -> list[Reminder]:
    """
    One notification per unit for documents that have just finished reading.

    Coalesced, and that is the whole design. The realistic sequence is: shoot,
    shoot, shoot, shoot, lock the phone, walk to a lecture. Four buzzes for one
    action is how a student turns notifications off for the entire app, so four
    photos filed into one unit is one notification naming the unit.

    Polling only runs while the app is open, which means the moment worth
    telling somebody about — their notes are ready to ask questions about — is
    reliably the moment nobody is looking at the screen.
    """
    rows = (
        await session.execute(
            select(Material, Unit.code)
            .join(Unit, Unit.id == Material.unit_id)
            .where(
                Material.user_id.in_(preferences.keys()),
                Material.extraction_status.in_(TERMINAL),
                Material.notified_at.is_(None),
                Material.deleted_at.is_(None),
                Material.updated_at >= moment - FINISHED_WINDOW,
                # `note` and `link` are never read, so they never finish and
                # there is nothing to announce.
                Material.kind.in_(EXTRACTABLE_KINDS),
            )
            .order_by(Material.updated_at)
        )
    ).all()

    if not rows:
        return []

    groups: dict[tuple[uuid.UUID, uuid.UUID], list[tuple[Material, str]]] = {}
    for material, unit_code in rows:
        groups.setdefault((material.user_id, material.unit_id), []).append(
            (material, unit_code)
        )

    reminders = []
    for (user_id, unit_id), members in groups.items():
        materials = [material for material, _ in members]
        unit_code = members[0][1]

        reminders.append(
            Reminder(
                user_id=user_id,
                kind="material",
                # Unique per batch. The newest id is enough: a fifth document
                # arriving later forms a different group with a different
                # newest, and `notified_at` is what actually stops a repeat.
                dedupe_key=f"material:{unit_id}:{max(str(m.id) for m in materials)}",
                title=_finished_title(materials, unit_code),
                body=_finished_body(materials),
                # The unit, not one material: a coalesced notification has no
                # single subject, and a tap that opens the unit puts every one
                # of them on screen.
                subject_id=unit_id,
                scheduled_for=moment,
                material_ids=[material.id for material in materials],
            )
        )

    return reminders


def _finished_title(materials: list[Material], unit_code: str) -> str:
    ready = [m for m in materials if m.extraction_status == "done"]

    if not ready:
        # Nothing succeeded, so leading with "ready" would be a lie.
        return f"{unit_code}: something needs your attention"

    pages = sum(m.page_count or 1 for m in ready)
    if len(ready) == 1:
        return f"{unit_code}: {materials[0].title[:40]} is ready"
    return f"{unit_code}: {pages} pages are ready"


def _finished_body(materials: list[Material]) -> str:
    """
    What the notification says under the title.

    The failures are named rather than counted. "1 could not be read" sends
    somebody into the app to hunt for which one; naming it means the sentence
    itself is the answer, and the card carries the reason when they get there.
    """
    ready = [m for m in materials if m.extraction_status == "done"]
    failed = [m for m in materials if m.extraction_status == "failed"]
    skipped = [m for m in materials if m.extraction_status == "skipped"]

    parts = []
    if ready:
        parts.append(
            "You can ask about it now."
            if len(ready) == 1
            else f"{len(ready)} documents are ready to ask about."
        )
    if failed:
        parts.append(
            f"{failed[0].title[:40]} could not be read."
            if len(failed) == 1
            else f"{len(failed)} could not be read."
        )
    if skipped:
        parts.append(
            f"{skipped[0].title[:40]} needs a bigger plan."
            if len(skipped) == 1
            else f"{len(skipped)} need a bigger plan."
        )

    return " ".join(parts)


async def _mark_notified(
    session: AsyncSession, reserved: list[tuple[Reminder, NotificationLog]], moment
) -> None:
    """
    Record that the student has been told, so the next sweep does not tell them
    again.

    Stamped after reserving rather than while building the list, so a batch
    dropped for quiet hours stays eligible — someone who files notes at 23:00
    hears about them in the morning rather than never.
    """
    ids = [
        material_id
        for reminder, _ in reserved
        for material_id in reminder.material_ids
    ]
    if not ids:
        return

    await session.execute(
        update(Material).where(Material.id.in_(ids)).values(notified_at=moment)
    )
    await session.commit()
