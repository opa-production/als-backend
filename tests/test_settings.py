"""
Preferences, devices, the streak counter and the usage meters.

The streak is the interesting one: it is derived from stored days rather than
incremented, so most of these assert that the derivation is right in the cases
a counter would quietly get wrong.
"""

import uuid
from datetime import date, timedelta

from app.services.plans import UNIT_HARD_CAP
from app.services.streak import compute, record_day
from tests.conftest import sign_in

# --- Preferences --------------------------------------------------------------


async def test_settings_have_sensible_defaults(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/me/settings", headers=headers)).json()
    assert body["deadline_reminders"] is True
    assert body["biometric_lock"] is False
    assert body["timezone"] == "Africa/Nairobi"


async def test_a_patch_leaves_other_settings_alone(client):
    headers, _ = await sign_in(client)

    await client.patch(
        "/api/v1/me/settings", json={"deadline_reminders": False}, headers=headers
    )
    body = (
        await client.patch(
            "/api/v1/me/settings",
            json={"biometric_lock": True, "biometric_kind": "face"},
            headers=headers,
        )
    ).json()

    assert body["biometric_lock"] is True
    # The earlier change must survive the later one.
    assert body["deadline_reminders"] is False


async def test_quiet_hours_must_look_like_a_time(client):
    headers, _ = await sign_in(client)

    bad = await client.patch(
        "/api/v1/me/settings", json={"quiet_hours_start": "10pm"}, headers=headers
    )
    assert bad.status_code == 422


# --- Devices ------------------------------------------------------------------


async def test_registering_the_same_device_twice_updates_one_row(client):
    headers, _ = await sign_in(client)
    device_id = str(uuid.uuid4())

    first = await client.put(
        "/api/v1/me/devices",
        json={"id": device_id, "platform": "android", "push_token": "tok_1"},
        headers=headers,
    )
    second = await client.put(
        "/api/v1/me/devices",
        json={"id": device_id, "platform": "android", "app_version": "1.1.0"},
        headers=headers,
    )

    assert first.json()["has_push"] is True
    assert second.json()["id"] == device_id
    assert second.json()["app_version"] == "1.1.0"
    # Omitting the token must not clear it.
    assert second.json()["has_push"] is True


async def test_forgetting_a_device_clears_only_the_token(client):
    headers, _ = await sign_in(client)
    device_id = str(uuid.uuid4())

    await client.put(
        "/api/v1/me/devices",
        json={"id": device_id, "push_token": "tok_1"},
        headers=headers,
    )
    assert (
        await client.delete(f"/api/v1/me/devices/{device_id}", headers=headers)
    ).status_code == 204

    # Re-registering proves the row survived, which is what keeps per-device
    # sign-out working.
    again = await client.put(
        "/api/v1/me/devices", json={"id": device_id}, headers=headers
    )
    assert again.json()["has_push"] is False


async def test_someone_elses_device_is_not_yours_to_forget(client):
    headers, _ = await sign_in(client)

    response = await client.delete(
        f"/api/v1/me/devices/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 404


# --- Streak -------------------------------------------------------------------


async def test_recording_a_day_twice_counts_once(client):
    _, user_id = await sign_in(client)
    today = date(2026, 3, 10)

    async with client.sessions() as session:
        assert await record_day(session, user_id=user_id, day=today) is True
        assert await record_day(session, user_id=user_id, day=today) is False
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=today)

    assert streak.current == 1
    assert streak.total_days == 1


async def test_consecutive_days_build_a_streak(client):
    _, user_id = await sign_in(client)
    today = date(2026, 3, 10)

    async with client.sessions() as session:
        for offset in range(4):
            await record_day(
                session, user_id=user_id, day=today - timedelta(days=offset)
            )
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=today)

    assert streak.current == 4


async def test_a_gap_ends_the_streak(client):
    _, user_id = await sign_in(client)
    today = date(2026, 3, 10)

    async with client.sessions() as session:
        for day in (today, today - timedelta(days=1), today - timedelta(days=5)):
            await record_day(session, user_id=user_id, day=day)
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=today)

    assert streak.current == 2


async def test_a_day_ahead_of_the_server_still_counts(client):
    """
    The read side dates itself in UTC; the write side stores the student's own
    local day. East of UTC those disagree between midnight and the offset, so
    the newest stored day can be *ahead* of the server's `today`.

    That must not read as a broken streak. It used to score zero, not one: the
    run was anchored to `today` and simply refused to count when the newest day
    did not sit exactly on it or the day before.
    """
    _, user_id = await sign_in(client)

    # 00:30 in Nairobi on the 11th is still the 10th in UTC.
    local_today = date(2026, 3, 11)
    server_today = date(2026, 3, 10)

    async with client.sessions() as session:
        for offset in range(3):
            await record_day(
                session, user_id=user_id, day=local_today - timedelta(days=offset)
            )
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=server_today)

    assert streak.current == 3


async def test_not_having_revised_yet_today_keeps_yesterdays_run(client):
    """
    Opening the app at nine in the morning must not read as a broken streak.
    Only a gap before *yesterday* ends it.
    """
    _, user_id = await sign_in(client)
    today = date(2026, 3, 10)

    async with client.sessions() as session:
        for offset in (1, 2, 3):
            await record_day(
                session, user_id=user_id, day=today - timedelta(days=offset)
            )
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=today)

    assert streak.current == 3


async def test_the_longest_run_survives_a_break(client):
    """A broken streak still happened — starting again is not losing."""
    _, user_id = await sign_in(client)
    today = date(2026, 3, 20)

    async with client.sessions() as session:
        # A run of five, a gap, then a run of two ending today.
        for offset in (14, 13, 12, 11, 10, 1, 0):
            await record_day(
                session, user_id=user_id, day=today - timedelta(days=offset)
            )
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=today)

    assert streak.current == 2
    assert streak.longest == 5


async def test_this_week_is_monday_first(client):
    _, user_id = await sign_in(client)
    # 11 March 2026 is a Wednesday.
    wednesday = date(2026, 3, 11)

    async with client.sessions() as session:
        for day in (wednesday, wednesday - timedelta(days=1)):
            await record_day(session, user_id=user_id, day=day)
        # The Sunday before belongs to the previous week, not this one.
        await record_day(session, user_id=user_id, day=date(2026, 3, 8))
        await session.commit()

    async with client.sessions() as session:
        streak = await compute(session, user_id=user_id, today=wednesday)

    assert streak.this_week == [date(2026, 3, 10), wednesday]


async def test_the_streak_endpoint_records_and_reports(client):
    headers, _ = await sign_in(client)

    posted = await client.post(
        "/api/v1/me/streak", json={"day": "2026-03-10"}, headers=headers
    )
    assert posted.status_code == 200
    assert posted.json()["current"] == 1

    read = await client.get("/api/v1/me/streak?today=2026-03-10", headers=headers)
    assert read.json()["current"] == 1
    assert read.json()["last_day"] == "2026-03-10"


async def test_no_study_days_is_a_zero_streak_not_an_error(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/me/streak", headers=headers)).json()
    assert body["current"] == 0
    assert body["last_day"] is None


# --- Usage --------------------------------------------------------------------


async def test_usage_reports_the_free_meters(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/me/usage", headers=headers)).json()

    assert body["tier"] == "free"
    meter = body["ai_queries_this_month"]
    assert meter["used"] == 0
    assert meter["limit"] == 30
    assert meter["unlimited"] is False
    # The app draws "refills in N days" off this rather than doing calendar
    # arithmetic of its own.
    assert meter["resets_at"].endswith("-01")
    # A lifetime ceiling is not a reset, and must not be drawn as one.
    assert body["ai_queries_total"]["resets_at"] is None
    # The same ceiling on every tier, and never a countdown: no plan lifts it
    # and nothing about it refills.
    assert body["course_units"]["limit"] == UNIT_HARD_CAP
    assert body["course_units"]["unlimited"] is False
    assert body["course_units"]["resets_at"] is None
    # The page pool is the meter that now bounds a free account's filing. Both
    # figures are 100 on Free, and only the monthly one carries a reset date.
    assert body["pdf_pages_this_month"]["limit"] == 100
    assert body["pdf_pages_this_month"]["resets_at"].endswith("-01")
    assert body["pdf_pages_total"]["limit"] == 100
    assert body["pdf_pages_total"]["resets_at"] is None
    # No OCR on free, so the meter is a zero ceiling rather than a lie.
    assert body["ocr_pages_this_month"]["limit"] == 0


async def test_usage_needs_a_token(client):
    assert (await client.get("/api/v1/me/usage")).status_code == 401


def test_settings_also_read_the_server_settings_file() -> None:
    """
    Ops scripts inherit nothing over SSH.

    systemd hands /etc/als-backend/env to the API and the worker, but
    `scripts/create_admin.py` run by hand gets no environment at all — so it
    fell back to the built-in default connection string and tried to reach a
    database on localhost, on a box whose Postgres is in another country. The
    fix is for Settings to read that file itself; this pins it, because the
    symptom is remote and the cause is one line.
    """
    from app.core.config import Settings

    sources = Settings.model_config["env_file"]
    assert "/etc/als-backend/env" in sources, (
        "Settings no longer reads the server settings file, so every ops script "
        "run over SSH will silently fall back to the localhost default."
    )
    # .env must stay first: on a laptop it is the only file that exists, and a
    # developer's settings must never be overridden by a stray system file.
    assert sources[0] == ".env"
