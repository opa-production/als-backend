"""
Reminders: what goes out, when, and — mostly — what does not.

The failure mode this file exists for is not "no notification arrived". It is
the opposite: the sweep runs every minute, a deadline sits inside its lead
window for that entire window, and the naive version sends the same nudge sixty
times before the student can turn it off. So most of what is pinned here is
silence — already sent, opted out, inside quiet hours, still too far away.

The other half is timezones. Everything the sweep compares starts in a different
frame: a deadline is an instant, a class is a wall clock, quiet hours are a
preference in the student's own zone. The tests use Nairobi (UTC+3, no DST) so
an off-by-a-timezone is a three hour error and cannot hide.
"""

import itertools
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest

from app.models.account import Device
from app.models.course import ClassSession, Unit
from app.models.notification import NotificationLog
from app.models.planner import Event
from app.models.settings import UserSettings
from app.services import notifications
from app.services.push import PushMessage, PushResult
from tests.conftest import sign_in

TOKEN = "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]"

#: Keeps unit codes unique across materials filed in one test.
_UNIT_CODES = itertools.count(1)

#: 2026-09-01 is a Tuesday. 09:00 UTC is midday in Nairobi, which is outside
#: any default quiet window — so a test that gets silence got it for the reason
#: it was testing.
NOON_IN_NAIROBI = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


class FakeProvider:
    """Records what would have been sent, and answers however a test needs."""

    def __init__(self, *, ok=True, dead=False):
        self.sent: list[PushMessage] = []
        self._ok = ok
        self._dead = dead

    async def send(self, messages):
        self.sent.extend(messages)
        return PushResult(
            ok=[self._ok] * len(messages),
            errors=["" if self._ok else "rejected"] * len(messages),
            dead_tokens={m.token for m in messages} if self._dead else set(),
        )


async def _account(client, *, with_token=True, **preferences):
    """A signed-in student with a device and, optionally, a live push token."""
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        session.add(
            Device(
                id=uuid.uuid4(),
                user_id=user_id,
                platform="ios",
                push_token=TOKEN if with_token else None,
            )
        )
        if preferences:
            session.add(UserSettings(user_id=user_id, **preferences))
        await session.commit()

    return headers, user_id


async def _add_event(client, user_id, *, due_at, done=False, kind="assignment"):
    async with client.sessions() as session:
        event = Event(
            id=uuid.uuid4(),
            user_id=user_id,
            title="Compiler design report",
            kind=kind,
            due_at=due_at,
            done=done,
        )
        session.add(event)
        await session.commit()
        return event.id


async def _add_class(client, user_id, *, weekday, starts_at, room=""):
    async with client.sessions() as session:
        unit = Unit(id=uuid.uuid4(), user_id=user_id, code="CS201", title="Compilers")
        session.add(unit)
        await session.flush()
        slot = ClassSession(
            id=uuid.uuid4(),
            user_id=user_id,
            unit_id=unit.id,
            weekday=weekday,
            starts_at=starts_at,
            ends_at=time(10, 0),
            room=room,
        )
        session.add(slot)
        await session.commit()
        return slot.id


async def _sweep(client, *, now, provider):
    async with client.sessions() as session:
        return await notifications.sweep(
            session, client=None, now=now, provider=provider
        )


async def _log(client, user_id):
    from sqlalchemy import select

    async with client.sessions() as session:
        rows = await session.scalars(
            select(NotificationLog).where(NotificationLog.user_id == user_id)
        )
        return list(rows)


# --- Deadlines ----------------------------------------------------------------


async def test_a_deadline_inside_the_lead_window_is_sent(client):
    _, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1

    assert len(provider.sent) == 1
    assert "in 10 minutes" in provider.sent[0].title
    # The tap has to land on the right screen, not just open the app.
    assert provider.sent[0].data["kind"] == "deadline"


async def test_a_deadline_beyond_the_lead_window_waits(client):
    _, user_id = await _account(client)
    # Default lead is 15 minutes; this is two hours out.
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(hours=2))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_a_longer_lead_reaches_further_out(client):
    _, user_id = await _account(client, reminder_lead_minutes=180)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(hours=2))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert "in 2 hours" in provider.sent[0].title


async def test_the_same_deadline_is_not_sent_twice(client):
    """The one that matters: the sweep runs every minute."""
    _, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1

    for minute in range(1, 6):
        moment = NOON_IN_NAIROBI + timedelta(minutes=minute)
        assert await _sweep(client, now=moment, provider=provider) == 0

    assert len(provider.sent) == 1


async def test_moving_a_deadline_earns_a_fresh_reminder(client):
    """Because the due date is part of the key, not just the event id."""
    _, user_id = await _account(client)
    event_id = await _add_event(
        client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10)
    )

    provider = FakeProvider()
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    async with client.sessions() as session:
        event = await session.get(Event, event_id)
        event.due_at = NOON_IN_NAIROBI + timedelta(days=1, minutes=10)
        await session.commit()

    later = NOON_IN_NAIROBI + timedelta(days=1)
    assert await _sweep(client, now=later, provider=provider) == 1


async def test_a_finished_deadline_is_not_a_reminder(client):
    _, user_id = await _account(client)
    await _add_event(
        client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10), done=True
    )

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_a_deadline_already_past_is_not_a_reminder(client):
    """A nudge about something that has already gone is noise, not a service."""
    _, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI - timedelta(minutes=5))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_turning_deadline_reminders_off_stops_them(client):
    _, user_id = await _account(client, deadline_reminders=False)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_a_student_who_never_opened_settings_still_gets_reminders(client):
    """
    No settings row exists until the screen is read once. Defaulting to silence
    would mean the students least likely to touch Settings hear nothing.
    """
    _, user_id = await _account(client)  # no UserSettings row created
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1


async def test_a_device_with_no_token_is_not_reachable(client):
    _, user_id = await _account(client, with_token=False)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0
    # And nothing was claimed, so allowing notifications later still works.
    assert await _log(client, user_id) == []


# --- Classes ------------------------------------------------------------------


async def test_a_class_is_reminded_in_the_students_own_timezone(client):
    """
    The slot says 12:30. That is 12:30 in Nairobi, which is 09:30 UTC — so at
    09:00 UTC it is thirty minutes away, not three and a half hours.
    """
    _, user_id = await _account(client, reminder_lead_minutes=45)
    await _add_class(client, user_id, weekday=2, starts_at=time(12, 30), room="LR7")

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1

    assert "CS201" in provider.sent[0].title
    assert "LR7" in provider.sent[0].body


async def test_a_class_on_another_weekday_is_not_reminded(client):
    _, user_id = await _account(client, reminder_lead_minutes=45)
    # 2026-09-01 is a Tuesday, which is 2 on the model's Sunday-based scale.
    await _add_class(client, user_id, weekday=4, starts_at=time(12, 30))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_a_long_lead_reaches_tomorrows_first_class(client):
    """
    An eight o'clock lecture is asked about the evening before, which is a
    different local day — the case a same-day-only sweep misses every time.
    """
    _, user_id = await _account(client, reminder_lead_minutes=720)
    # Wednesday = 3.
    await _add_class(client, user_id, weekday=3, starts_at=time(8, 0))

    # 21:00 in Nairobi on Tuesday: eleven hours before the lecture.
    tuesday_evening = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
    provider = FakeProvider()
    assert await _sweep(client, now=tuesday_evening, provider=provider) == 1


async def test_the_same_class_is_not_sent_twice_in_a_day(client):
    _, user_id = await _account(client, reminder_lead_minutes=45)
    await _add_class(client, user_id, weekday=2, starts_at=time(12, 30))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert (
        await _sweep(
            client, now=NOON_IN_NAIROBI + timedelta(minutes=5), provider=provider
        )
        == 0
    )


async def test_the_same_class_is_reminded_again_next_week(client):
    """The key carries the local day, so a weekly slot recurs."""
    _, user_id = await _account(client, reminder_lead_minutes=45)
    await _add_class(client, user_id, weekday=2, starts_at=time(12, 30))

    provider = FakeProvider()
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    next_tuesday = NOON_IN_NAIROBI + timedelta(days=7)
    assert await _sweep(client, now=next_tuesday, provider=provider) == 1


async def test_turning_class_reminders_off_stops_them(client):
    _, user_id = await _account(
        client, class_reminders=False, reminder_lead_minutes=45
    )
    await _add_class(client, user_id, weekday=2, starts_at=time(12, 30))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


# --- Quiet hours --------------------------------------------------------------


async def test_nothing_is_sent_inside_quiet_hours(client):
    _, user_id = await _account(client)
    # 01:00 UTC is 04:00 in Nairobi, inside the default 22:00-06:00 window.
    small_hours = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    await _add_event(client, user_id, due_at=small_hours + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=small_hours, provider=provider) == 0


async def test_a_reminder_silenced_by_quiet_hours_is_not_marked_sent(client):
    """
    It must stay eligible. Recording it would mean a deadline silenced at 04:00
    is never mentioned again, including at nine in the morning when it is still
    ahead — which is precisely when the student needed it.
    """
    _, user_id = await _account(client, reminder_lead_minutes=1440)
    small_hours = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    await _add_event(client, user_id, due_at=small_hours + timedelta(hours=12))

    provider = FakeProvider()
    await _sweep(client, now=small_hours, provider=provider)
    assert await _log(client, user_id) == []

    # 06:00 UTC is 09:00 in Nairobi — the window has lifted.
    assert (
        await _sweep(
            client, now=datetime(2026, 9, 1, 6, 0, tzinfo=UTC), provider=provider
        )
        == 1
    )


async def test_equal_quiet_bounds_mean_no_quiet_hours(client):
    """
    Reading 22:00-22:00 as a 24-hour silence would turn a fumbled setting into
    an app that never notifies and gives no reason.
    """
    _, user_id = await _account(client, quiet_hours_start="22:00", quiet_hours_end="22:00")
    small_hours = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    await _add_event(client, user_id, due_at=small_hours + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=small_hours, provider=provider) == 1


@pytest.mark.parametrize(
    ("start", "end", "local_hour", "quiet"),
    [
        ("22:00", "06:00", 23, True),   # after the start, before midnight
        ("22:00", "06:00", 3, True),    # after midnight, before the end
        ("22:00", "06:00", 12, False),
        ("13:00", "14:00", 13, True),   # a window inside one day
        ("13:00", "14:00", 15, False),
    ],
)
def test_quiet_window_boundaries(start, end, local_hour, quiet):
    preference = UserSettings(
        quiet_hours_start=start, quiet_hours_end=end, timezone="UTC"
    )
    moment = datetime(2026, 9, 1, local_hour, 0, tzinfo=UTC)

    assert notifications._in_quiet_hours(preference, moment) is quiet


def test_an_unknown_timezone_does_not_stop_notifications(client):
    """A typo the client let through is a preference, not an outage."""
    preference = UserSettings(
        quiet_hours_start="22:00", quiet_hours_end="06:00", timezone="Mars/Olympus"
    )

    assert notifications._zone(preference).key == "UTC"


# --- Delivery -----------------------------------------------------------------


async def test_a_rejected_send_is_recorded_as_failed(client):
    """
    Silence is how push fails. A row saying `failed` is the difference between
    a token Expo refused and a notification nobody happened to tap.
    """
    _, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider(ok=False)
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0

    rows = await _log(client, user_id)
    assert [row.status for row in rows] == ["failed"]
    assert rows[0].sent_at is None


async def test_a_dead_token_is_cleared(client):
    """Otherwise the same uninstalled app is pushed to every minute, forever."""
    _, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider(ok=False, dead=True)
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    from sqlalchemy import select

    async with client.sessions() as session:
        tokens = list(
            await session.scalars(
                select(Device.push_token).where(Device.user_id == user_id)
            )
        )
    # Sign-in registers a device of its own, so the assertion is that no live
    # token survives — not that there is exactly one row.
    assert not any(tokens)


async def test_a_malformed_token_is_never_sent_to(client):
    """A device that was denied permission sometimes registers junk."""
    headers, user_id = await sign_in(client)
    async with client.sessions() as session:
        session.add(
            Device(id=uuid.uuid4(), user_id=user_id, push_token="not-a-real-token")
        )
        await session.commit()

    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_every_device_on_the_account_hears_it_once(client):
    """A phone and a tablet are two sends and one reminder."""
    _, user_id = await _account(client)
    async with client.sessions() as session:
        session.add(
            Device(
                id=uuid.uuid4(),
                user_id=user_id,
                push_token="ExponentPushToken[second-device-token]",
            )
        )
        await session.commit()

    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert len(provider.sent) == 2
    assert len(await _log(client, user_id)) == 1


async def test_one_students_reminder_never_reaches_another(client):
    from tests.conftest import OTHER_PHONE

    _, mine = await _account(client)
    _, theirs = await sign_in(client, phone=OTHER_PHONE)
    async with client.sessions() as session:
        session.add(
            Device(
                id=uuid.uuid4(),
                user_id=theirs,
                push_token="ExponentPushToken[someone-else]",
            )
        )
        await session.commit()

    await _add_event(client, mine, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))

    provider = FakeProvider()
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    assert [message.token for message in provider.sent] == [TOKEN]


# --- The endpoints ------------------------------------------------------------


async def test_the_test_push_reports_when_there_is_no_device(client):
    headers, _ = await sign_in(client)

    body = (await client.post("/api/v1/me/push/test", headers=headers)).json()
    assert body == {"delivered": 0, "has_devices": False}


async def test_the_test_push_goes_to_a_registered_device(client):
    headers, _ = await _account(client)

    body = (await client.post("/api/v1/me/push/test", headers=headers)).json()
    # No Expo credentials in tests, so the console provider takes it — which is
    # exactly what a developer sees locally.
    assert body == {"delivered": 1, "has_devices": True}


async def test_the_notification_list_shows_what_was_sent(client):
    headers, user_id = await _account(client)
    await _add_event(client, user_id, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))
    await _sweep(client, now=NOON_IN_NAIROBI, provider=FakeProvider())

    body = (await client.get("/api/v1/me/notifications", headers=headers)).json()
    assert len(body) == 1
    assert body[0]["kind"] == "deadline"
    assert body[0]["status"] == "sent"


async def test_the_notification_list_is_scoped_to_the_account(client):
    from tests.conftest import OTHER_PHONE

    _, mine = await _account(client)
    await _add_event(client, mine, due_at=NOON_IN_NAIROBI + timedelta(minutes=10))
    await _sweep(client, now=NOON_IN_NAIROBI, provider=FakeProvider())

    other_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    body = (await client.get("/api/v1/me/notifications", headers=other_headers)).json()
    assert body == []


# --- Documents that finished --------------------------------------------------
#
# Polling only runs while the app is open, so the moment worth telling somebody
# about — their notes are ready to ask questions about — is reliably the moment
# nobody is looking at the screen. These pin the shape of that notification, and
# most of what they pin is *restraint*: four photos filed in one sitting must be
# one buzz, not four, and nothing stale must ever be announced.


async def _add_material(
    client,
    user_id,
    *,
    unit_id=None,
    unit_code="CS201",
    status="done",
    title="Week 4 notes",
    finished_at=None,
    pages=1,
):
    from app.models.knowledge import Material

    async with client.sessions() as session:
        if unit_id is None:
            # Unique per call: `units` has UNIQUE(user_id, code), so two
            # materials filed under the default code would collide.
            code = f"{unit_code}{next(_UNIT_CODES)}"
            unit = Unit(id=uuid.uuid4(), user_id=user_id, code=code, title="C")
            session.add(unit)
            await session.flush()
            unit_id = unit.id

        material = Material(
            id=uuid.uuid4(),
            user_id=user_id,
            unit_id=unit_id,
            kind="pdf",
            title=title,
            extraction_status=status,
            extraction_error=None if status == "done" else "Could not read that.",
            page_count=pages,
            updated_at=finished_at or NOON_IN_NAIROBI,
        )
        session.add(material)
        await session.commit()
        return unit_id, material.id


async def test_a_finished_document_is_announced(client):
    _, user_id = await _account(client)
    await _add_material(client, user_id)
    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert "ready" in provider.sent[0].title


async def test_four_photos_in_one_unit_are_one_notification(client):
    """
    The rule that decides whether notifications stay switched on.

    Shoot, shoot, shoot, shoot, lock the phone. Four buzzes for one action is
    how somebody turns them off for the entire app.
    """
    _, user_id = await _account(client)
    unit_id, _ = await _add_material(client, user_id, title="Page 1")
    for page in range(2, 5):
        await _add_material(client, user_id, unit_id=unit_id, title=f"Page {page}")

    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert len(provider.sent) == 1
    assert "4 pages are ready" in provider.sent[0].title


async def test_a_notification_held_by_quiet_hours_is_not_lost_to_staleness(client):
    """
    The reason `FINISHED_WINDOW` is a day rather than the six hours it started
    as.

    Quiet hours delay a notification; the freshness window drops one. Set the
    window shorter than the longest quiet period and the first quietly becomes
    the second — notes filed at 22:30 are held until 06:00 and then discarded
    for being too old, so the student is never told at all.
    """
    from app.services.notifications import FINISHED_WINDOW

    _, user_id = await _account(client)
    # 22:30 Nairobi, near the start of the default 22:00-06:00 window.
    night = datetime(2026, 9, 1, 19, 30, tzinfo=UTC)
    await _add_material(client, user_id, finished_at=night)

    provider = FakeProvider()
    assert await _sweep(client, now=night, provider=provider) == 0

    # 07:00 Nairobi the next morning: the longest a default quiet window can
    # hold anything.
    morning = night + timedelta(hours=8, minutes=30)
    assert morning - night < FINISHED_WINDOW
    assert await _sweep(client, now=morning, provider=provider) == 1


async def test_two_units_are_two_notifications(client):
    """Coalescing is per unit, not per sweep — they are different subjects."""
    _, user_id = await _account(client)
    await _add_material(client, user_id, unit_code="CS201")
    await _add_material(client, user_id, unit_code="MAT204")

    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 2


async def test_a_student_is_told_once(client):
    """
    The sweep runs every minute and the row stays `done` for ever.

    This is the same failure the deadline reminders exist to prevent, arriving
    by a different route.
    """
    _, user_id = await _account(client)
    await _add_material(client, user_id)
    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0
    later = NOON_IN_NAIROBI + timedelta(minutes=30)
    assert await _sweep(client, now=later, provider=provider) == 0


async def test_a_failure_is_announced_too(client):
    """
    `failed` and `skipped` are terminal and need the student to do something.

    They are also the ones that currently sit unnoticed for days: unlike `done`,
    there is no other moment when somebody finds out.
    """
    _, user_id = await _account(client)
    await _add_material(client, user_id, status="failed", title="Blurry page")

    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 1
    message = provider.sent[0]
    # Not "ready" — nothing succeeded, and saying so would be a lie.
    assert "ready" not in message.title
    assert "Blurry page" in message.body


async def test_a_mixed_batch_leads_with_what_worked(client):
    _, user_id = await _account(client)
    unit_id, _ = await _add_material(client, user_id, title="Page 1")
    await _add_material(client, user_id, unit_id=unit_id, title="Page 2")
    await _add_material(
        client, user_id, unit_id=unit_id, status="failed", title="Blurry"
    )

    provider = FakeProvider()
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    body = provider.sent[0].body
    assert "2 documents are ready" in body
    # Named, not counted: "1 could not be read" sends someone hunting.
    assert "Blurry could not be read" in body


async def test_a_document_still_being_read_is_not_announced(client):
    _, user_id = await _account(client)
    await _add_material(client, user_id, status="pending")
    await _add_material(client, user_id, status="running")

    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_last_terms_coursework_is_not_announced(client):
    """
    "Your notes are ready" is only true near the moment it becomes true.

    Without the freshness window, deploying this would have announced every
    document in the table — including coursework filed in July.
    """
    _, user_id = await _account(client)
    await _add_material(
        client, user_id, finished_at=NOON_IN_NAIROBI - timedelta(days=40)
    )

    provider = FakeProvider()

    assert await _sweep(client, now=NOON_IN_NAIROBI, provider=provider) == 0


async def test_quiet_hours_delay_rather_than_cancel(client):
    """
    Someone who files notes at 23:00 hears about it in the morning.

    `notified_at` is stamped on reserve, not while building the batch, which is
    what keeps a silenced notification eligible instead of losing it.
    """
    _, user_id = await _account(client)
    # 23:30 in Nairobi, inside the default 22:00–06:00 window.
    night = datetime(2026, 9, 1, 20, 30, tzinfo=UTC)
    await _add_material(client, user_id, finished_at=night)

    provider = FakeProvider()
    assert await _sweep(client, now=night, provider=provider) == 0

    morning = night + timedelta(hours=8)
    assert await _sweep(client, now=morning, provider=provider) == 1


async def test_the_tap_opens_the_unit(client):
    """
    A coalesced notification has no single subject, and a tap that opens the
    unit puts every document in the batch on screen.
    """
    _, user_id = await _account(client)
    unit_id, _ = await _add_material(client, user_id)

    provider = FakeProvider()
    await _sweep(client, now=NOON_IN_NAIROBI, provider=provider)

    assert provider.sent[0].data == {"kind": "material", "id": str(unit_id)}
