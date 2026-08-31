"""
Sync: the parts that are easy to get subtly wrong.

Idempotency, conflict resolution, tombstones and ownership. Every one of these
fails silently rather than loudly if it regresses — a duplicate row or a lost
edit does not raise, it just quietly ruins someone's semester.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.models.knowledge import Material
from app.services.plans import UNIT_HARD_CAP, Tier
from tests.conftest import OTHER_PHONE, give_plan, sign_in


def _unit(unit_id=None, *, code="CS201", title="Data Structures", updated=None, deleted=None):
    return {
        "id": str(unit_id or uuid.uuid4()),
        "code": code,
        "title": title,
        "lecturer": "",
        "updated_at": (updated or datetime.now(UTC)).isoformat(),
        "deleted_at": deleted.isoformat() if deleted else None,
    }


async def test_push_then_pull_round_trips(client):
    headers, _ = await sign_in(client)
    unit = _unit()

    pushed = await client.post(
        "/api/v1/sync", json={"units": [unit]}, headers=headers
    )
    assert pushed.status_code == 200
    assert pushed.json()["units"]["applied"] == 1

    pulled = await client.get("/api/v1/sync", headers=headers)
    assert pulled.status_code == 200
    units = pulled.json()["units"]
    assert len(units) == 1
    assert units[0]["code"] == "CS201"


async def test_pushing_the_same_row_twice_changes_nothing(client):
    headers, _ = await sign_in(client)
    unit = _unit()

    first = await client.post("/api/v1/sync", json={"units": [unit]}, headers=headers)
    second = await client.post("/api/v1/sync", json={"units": [unit]}, headers=headers)

    assert first.json()["units"]["applied"] == 1
    # Same timestamp, so the replay is a no-op rather than a second write.
    assert second.json()["units"]["skipped"] == 1

    pulled = await client.get("/api/v1/sync", headers=headers)
    assert len(pulled.json()["units"]) == 1


async def test_a_newer_edit_wins(client):
    headers, _ = await sign_in(client)
    unit_id = uuid.uuid4()
    now = datetime.now(UTC)

    await client.post(
        "/api/v1/sync",
        json={"units": [_unit(unit_id, title="Old", updated=now)]},
        headers=headers,
    )
    await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(unit_id, title="New", updated=now + timedelta(minutes=1))
            ]
        },
        headers=headers,
    )

    pulled = await client.get("/api/v1/sync", headers=headers)
    assert pulled.json()["units"][0]["title"] == "New"


async def test_a_stale_edit_is_refused(client):
    """A phone that has been in a drawer must not overwrite newer work."""
    headers, _ = await sign_in(client)
    unit_id = uuid.uuid4()
    now = datetime.now(UTC)

    await client.post(
        "/api/v1/sync",
        json={"units": [_unit(unit_id, title="Current", updated=now)]},
        headers=headers,
    )
    stale = await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(unit_id, title="Stale", updated=now - timedelta(hours=5))
            ]
        },
        headers=headers,
    )

    assert stale.json()["units"]["skipped"] == 1
    pulled = await client.get("/api/v1/sync", headers=headers)
    assert pulled.json()["units"][0]["title"] == "Current"


async def test_a_deletion_travels_as_a_tombstone(client):
    """A row that simply vanished would be pushed straight back by a device."""
    headers, _ = await sign_in(client)
    unit_id = uuid.uuid4()
    now = datetime.now(UTC)

    await client.post(
        "/api/v1/sync", json={"units": [_unit(unit_id, updated=now)]}, headers=headers
    )
    await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(unit_id, updated=now + timedelta(seconds=1), deleted=now)
            ]
        },
        headers=headers,
    )

    units = (await client.get("/api/v1/sync", headers=headers)).json()["units"]
    assert len(units) == 1
    assert units[0]["deleted_at"] is not None


async def test_the_cursor_only_returns_what_changed(client):
    headers, user_id = await sign_in(client)
    # A paid plan, so nothing here is measuring an entitlement. Units are not
    # capped by tier any more, but the rest of the fixture reads clearer with a
    # student who is plainly not up against any limit.
    await give_plan(client, user_id, Tier.PRO)
    now = datetime.now(UTC)

    await client.post(
        "/api/v1/sync", json={"units": [_unit(updated=now)]}, headers=headers
    )
    first = await client.get("/api/v1/sync", headers=headers)
    cursor = first.json()["cursor"]

    # Nothing has changed since, so a second pull is empty.
    again = await client.get(f"/api/v1/sync?since={cursor}", headers=headers)
    assert again.json()["units"] == []

    await client.post(
        "/api/v1/sync",
        json={"units": [_unit(code="MAT204", updated=now + timedelta(minutes=5))]},
        headers=headers,
    )
    third = await client.get(f"/api/v1/sync?since={cursor}", headers=headers)
    assert len(third.json()["units"]) == 1
    assert third.json()["units"][0]["code"] == "MAT204"


async def test_one_student_never_sees_another(client):
    mine, _ = await sign_in(client)
    theirs, _ = await sign_in(client, phone=OTHER_PHONE)

    await client.post("/api/v1/sync", json={"units": [_unit()]}, headers=mine)

    assert (await client.get("/api/v1/sync", headers=theirs)).json()["units"] == []


async def test_the_unit_cap_rejects_the_row_not_the_request(client):
    """
    Over the ceiling, the extra units are refused — and everything else in the
    same push still has to land.

    The cap is `UNIT_HARD_CAP` on every tier, Free included, so this pushes one
    more than that rather than one more than a plan allowance. A free student
    filing five or six units for their real semester is the normal case and
    must never reach this path.
    """
    headers, _ = await sign_in(client)
    now = datetime.now(UTC)

    response = await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(code=f"AAA{index:03d}", updated=now)
                for index in range(UNIT_HARD_CAP + 1)
            ],
            "events": [
                {
                    "id": str(uuid.uuid4()),
                    "title": "Essay",
                    "kind": "assignment",
                    "label": "",
                    "due_at": None,
                    "done": False,
                    "updated_at": now.isoformat(),
                    "deleted_at": None,
                }
            ],
        },
        headers=headers,
    )

    body = response.json()
    assert body["units"]["applied"] == UNIT_HARD_CAP
    assert len(body["units"]["rejected"]) == 1
    # The message must not send them to the paywall: no plan lifts this.
    assert "plan" not in body["units"]["rejected"][0]
    # The event is not held hostage by the rejected unit.
    assert body["events"]["applied"] == 1


async def test_editing_an_existing_unit_is_never_capped(client):
    """
    Renaming a unit you already have must never be refused, even at the cap.

    Filled right to `UNIT_HARD_CAP` first, because a test that edits one of two
    units when the ceiling is ten proves nothing about the guard.
    """
    headers, _ = await sign_in(client)
    now = datetime.now(UTC)
    first = uuid.uuid4()

    await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(
                    first if index == 0 else uuid.uuid4(),
                    code=f"AAA{index:03d}",
                    updated=now,
                )
                for index in range(UNIT_HARD_CAP)
            ]
        },
        headers=headers,
    )

    # At the cap, but this is an edit, not a new unit.
    edited = await client.post(
        "/api/v1/sync",
        json={
            "units": [
                _unit(first, code="AAA000", title="Renamed", updated=now + timedelta(minutes=1))
            ]
        },
        headers=headers,
    )

    assert edited.json()["units"]["applied"] == 1
    assert edited.json()["units"]["rejected"] == []


async def test_chats_carry_their_messages_and_do_not_duplicate(client):
    headers, _ = await sign_in(client)
    now = datetime.now(UTC)
    chat_id = uuid.uuid4()
    message_id = uuid.uuid4()

    payload = {
        "chats": [
            {
                "id": str(chat_id),
                "title": "How do hash tables work?",
                "unit_id": None,
                "updated_at": now.isoformat(),
                "deleted_at": None,
                "messages": [
                    {
                        "id": str(message_id),
                        "role": "student",
                        "content": "How do hash tables work?",
                        "sources": None,
                        "created_at": now.isoformat(),
                    }
                ],
            }
        ]
    }

    await client.post("/api/v1/sync", json=payload, headers=headers)
    # Replaying must not duplicate the message.
    await client.post("/api/v1/sync", json=payload, headers=headers)

    chats = (await client.get("/api/v1/sync", headers=headers)).json()["chats"]
    assert len(chats) == 1
    assert len(chats[0]["messages"]) == 1


async def test_sync_needs_a_token(client):
    assert (await client.post("/api/v1/sync", json={})).status_code == 401
    assert (await client.get("/api/v1/sync")).status_code == 401


async def test_an_empty_since_means_a_first_run(client):
    """
    `?since=` is what a client builds from a null cursor, and it must mean
    "give me everything" rather than 422.

    This is the first sync a device ever attempts -- the one run where there is
    genuinely nothing to sync from -- and rejecting it produced
    `since: Input should be a valid datetime or date, input too short`, which
    points at the date parser rather than at the missing cursor.
    """
    headers, _ = await sign_in(client)

    await client.post("/api/v1/sync", json={"units": [_unit()]}, headers=headers)

    blank = await client.get("/api/v1/sync?since=", headers=headers)
    assert blank.status_code == 200, blank.text

    omitted = await client.get("/api/v1/sync", headers=headers)
    assert omitted.status_code == 200

    # Not merely accepted -- it has to behave as no cursor at all.
    assert blank.json()["units"] == omitted.json()["units"]
    assert len(blank.json()["units"]) == 1


async def test_whitespace_only_since_is_also_a_first_run(client):
    headers, _ = await sign_in(client)
    assert (await client.get("/api/v1/sync?since=%20", headers=headers)).status_code == 200


async def test_a_genuinely_malformed_since_is_still_rejected(client):
    """Being liberal about empty must not become being liberal about wrong."""
    headers, _ = await sign_in(client)
    bad = await client.get("/api/v1/sync?since=not-a-date", headers=headers)
    assert bad.status_code == 422
    assert "since" in bad.json()["message"]


async def test_a_long_title_is_clipped_rather_than_failing_the_whole_push(client):
    """
    Sync is all-or-nothing, so one overlong title used to 422 the entire batch.

    A device with a single bad material then synced nothing at all, forever --
    retrying every few seconds and failing identically each time. In production
    that showed up as `materials.0.title: String should have at most 300
    characters`, repeating in the log.
    """
    headers, _ = await sign_in(client)

    unit = _unit()
    await client.post("/api/v1/sync", json={"units": [unit]}, headers=headers)

    long_title = "A" * 450
    pushed = await client.post(
        "/api/v1/sync",
        json={
            "materials": [
                {
                    "id": str(uuid.uuid4()),
                    "unit_id": unit["id"],
                    "kind": "note",
                    "title": long_title,
                    "body": "",
                    "archived": False,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "deleted_at": None,
                }
            ]
        },
        headers=headers,
    )
    assert pushed.status_code == 200, pushed.text

    materials = (await client.get("/api/v1/sync", headers=headers)).json()["materials"]
    assert len(materials) == 1
    assert materials[0]["title"] == "A" * 300


async def test_a_failed_extraction_reaches_the_device_with_its_reason(client):
    """
    The device only ever learns about extraction through a pull — there is no
    status endpoint to poll, and `/materials/complete` answers once and never
    again.

    So a status with no reason attached is a card that can only say "not done".
    An app that renders anything other than `done` as "still reading" then shows
    a permanent spinner over a document that was rejected minutes ago with a
    perfectly good explanation sitting unread in a column.
    """
    headers, user_id = await sign_in(client)
    now = datetime.now(UTC)

    unit_id = uuid.uuid4()
    await client.post(
        "/api/v1/sync",
        json={"units": [_unit(unit_id, updated=now)]},
        headers=headers,
    )

    material_id = uuid.uuid4()
    async with client.sessions() as session:
        session.add(
            Material(
                id=material_id,
                user_id=user_id,
                unit_id=unit_id,
                kind="pdf",
                title="Lecture 4",
                extraction_status="failed",
                extraction_error="That PDF is password protected.",
            )
        )
        await session.commit()

    pulled = (await client.get("/api/v1/sync", headers=headers)).json()
    material = next(row for row in pulled["materials"] if row["id"] == str(material_id))

    assert material["extraction_status"] == "failed"
    assert material["extraction_error"] == "That PDF is password protected."


async def test_a_device_cannot_write_an_extraction_result(client):
    """
    Status and error are the server's to set. A device claiming `done` would be
    claiming its file had been read and indexed, which would make the tutor
    answer from a document it has never seen.
    """
    headers, user_id = await sign_in(client)
    now = datetime.now(UTC)

    unit_id = uuid.uuid4()
    material_id = uuid.uuid4()

    await client.post(
        "/api/v1/sync",
        json={
            "units": [_unit(unit_id, updated=now)],
            "materials": [
                {
                    "id": str(material_id),
                    "unit_id": str(unit_id),
                    "kind": "pdf",
                    "title": "Lecture 4",
                    "body": "",
                    "archived": False,
                    "updated_at": now.isoformat(),
                    "deleted_at": None,
                    # Both ignored: neither is a field sync copies.
                    "extraction_status": "done",
                    "extraction_error": "",
                }
            ],
        },
        headers=headers,
    )

    async with client.sessions() as session:
        material = await session.get(Material, material_id)

    assert material.extraction_status == "pending"
