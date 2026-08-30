"""
The feedback box.

Two audiences and one row: a student sends a paragraph, and the console reads
it. What is worth testing is the boundary between them — one student must not
see another's, and a student must not be able to fill the table.
"""


from tests.conftest import OTHER_PHONE, sign_in
from tests.test_admin import admin_headers

PARAGRAPH = (
    "Please let me download my notes as audio so I can revise in the matatu. "
    "My data runs out before the end of the month."
)


async def test_a_student_can_ask_for_a_feature(client):
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/me/feature-requests",
        json={"body": PARAGRAPH, "app_version": "1.4.2", "platform": "android"},
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["body"] == PARAGRAPH
    assert body["created_at"]


async def test_the_request_comes_back_to_the_student_who_sent_it(client):
    """The profile screen shows what you asked for, so you do not ask twice."""
    headers, _ = await sign_in(client)
    await client.post(
        "/api/v1/me/feature-requests", json={"body": PARAGRAPH}, headers=headers
    )

    listed = await client.get("/api/v1/me/feature-requests", headers=headers)

    assert listed.status_code == 200
    assert [row["body"] for row in listed.json()] == [PARAGRAPH]


async def test_one_student_never_sees_another_students_requests(client):
    """
    Not a public board. Anything else here is a forum, and a forum is a thing
    that has to be moderated before it is a thing that is useful.
    """
    mine, _ = await sign_in(client)
    theirs, _ = await sign_in(client, phone=OTHER_PHONE)

    await client.post(
        "/api/v1/me/feature-requests", json={"body": PARAGRAPH}, headers=theirs
    )

    listed = await client.get("/api/v1/me/feature-requests", headers=mine)
    assert listed.json() == []


async def test_a_stray_tap_is_not_a_feature_request(client):
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/me/feature-requests", json={"body": "pls"}, headers=headers
    )

    assert response.status_code == 400


async def test_a_student_cannot_fill_the_table(client):
    """
    A retry loop in the app is the realistic version of this, not a person
    typing. Either way the fifth is the last one today.
    """
    headers, _ = await sign_in(client)

    for _ in range(5):
        accepted = await client.post(
            "/api/v1/me/feature-requests",
            json={"body": PARAGRAPH},
            headers=headers,
        )
        assert accepted.status_code == 201

    refused = await client.post(
        "/api/v1/me/feature-requests", json={"body": PARAGRAPH}, headers=headers
    )
    assert refused.status_code == 429


async def test_anonymous_cannot_send_one(client):
    response = await client.post(
        "/api/v1/me/feature-requests", json={"body": PARAGRAPH}
    )
    assert response.status_code == 401


# --- The console --------------------------------------------------------------


async def test_the_console_reads_requests_with_who_asked(client):
    headers, user_id = await sign_in(client)
    await client.post(
        "/api/v1/me/feature-requests",
        json={"body": PARAGRAPH, "platform": "android"},
        headers=headers,
    )

    admin = await admin_headers(client)
    listed = await client.get(
        "/api/v1/admin/feedback/feature-requests", headers=admin
    )

    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1

    row = page["items"][0]
    assert row["body"] == PARAGRAPH
    assert row["user_id"] == str(user_id)
    assert row["platform"] == "android"
    # Nobody has paid, so the requester reads as Free rather than as nothing.
    assert row["tier"] == "free"


async def test_the_console_can_search_the_text(client):
    """"Has anyone else asked for this" is the question this table answers."""
    headers, _ = await sign_in(client)
    for body in (PARAGRAPH, "The timetable should sync with my Google calendar."):
        await client.post(
            "/api/v1/me/feature-requests", json={"body": body}, headers=headers
        )

    admin = await admin_headers(client)
    found = await client.get(
        "/api/v1/admin/feedback/feature-requests?search=audio", headers=admin
    )

    assert found.json()["total"] == 1
    assert "audio" in found.json()["items"][0]["body"]


async def test_the_console_can_filter_to_one_student(client):
    mine, my_id = await sign_in(client)
    theirs, _ = await sign_in(client, phone=OTHER_PHONE)

    for headers in (mine, theirs):
        await client.post(
            "/api/v1/me/feature-requests", json={"body": PARAGRAPH}, headers=headers
        )

    admin = await admin_headers(client)
    page = (
        await client.get(
            f"/api/v1/admin/feedback/feature-requests?user_id={my_id}",
            headers=admin,
        )
    ).json()

    assert page["total"] == 1
    assert page["items"][0]["user_id"] == str(my_id)


async def test_a_student_token_cannot_read_the_console(client):
    headers, _ = await sign_in(client)

    response = await client.get(
        "/api/v1/admin/feedback/feature-requests", headers=headers
    )
    assert response.status_code in (401, 403)


async def test_deleting_an_account_takes_its_requests_with_it(client):
    """
    Cascade, not a sweep. A paragraph tied to a deleted account is a record of
    a person who asked to be forgotten.
    """
    from sqlalchemy import delete, func, select

    from app.models.account import User
    from app.models.feedback import FeatureRequest

    headers, user_id = await sign_in(client)
    await client.post(
        "/api/v1/me/feature-requests", json={"body": PARAGRAPH}, headers=headers
    )

    async with client.sessions() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    async with client.sessions() as session:
        left = await session.scalar(
            select(func.count()).select_from(FeatureRequest)
        )

    assert left == 0
