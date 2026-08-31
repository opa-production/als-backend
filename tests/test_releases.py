"""
The update check.

Two things here are worth more than the rest. Version comparison, because
comparing versions as text is wrong in a way that only shows up at 1.10 — long
after the code shipped and looked fine. And the rule that being behind is not
the same as being locked out: forcing an update interrupts somebody mid-
revision, and it must only ever happen because a person decided it should.
"""

import pytest

from app.models.release import AppRelease
from app.services.releases import check, is_older, parse
from tests.conftest import sign_in
from tests.test_admin import admin_headers


def _release(**overrides):
    row = {
        "platform": "android",
        "version": "1.4.0",
        "minimum_version": "",
        "store_url": "",
        "notes": "",
        "published": True,
    }
    row.update(overrides)
    return AppRelease(**row)


# --- Comparison ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        # The one that matters. As text, "1.10.0" sorts before "1.9.0".
        ("1.9.0", "1.10.0"),
        ("1.9.9", "1.10.0"),
        ("2.0.0", "10.0.0"),
        # A shorter version is older than a longer one that starts the same way.
        ("1.4", "1.4.1"),
        # A missing or unreadable version is treated as ancient.
        ("", "1.0.0"),
        (None, "1.0.0"),
    ],
)
def test_versions_compare_as_numbers(older, newer):
    assert is_older(older, newer)
    assert not is_older(newer, older)


def test_the_same_version_is_not_older_than_itself():
    assert not is_older("1.4.0", "1.4.0")
    assert not is_older("1.4.0", "")


def test_a_prerelease_is_not_sent_backwards():
    """
    A beta is the *later* build, whatever semver says about ordering.

    Someone on 1.4.0-beta.2 holds something newer than 1.4.0, and telling them
    to update to the release they are ahead of is a loop.
    """
    assert parse("1.4.0-beta.2") == (1, 4, 0, 2)
    assert not is_older("1.4.0-beta.2", "1.4.0")


# --- The check ----------------------------------------------------------------


async def test_nothing_published_says_nothing(client):
    """The state this ships in. An empty table must not prompt anybody."""
    async with client.sessions() as session:
        result = await check(session, platform="android", version="1.0.0")

    assert result.update_available is False
    assert result.update_required is False
    assert result.latest_version == ""


async def test_being_behind_offers_but_does_not_force(client):
    """
    The default, and the important one.

    A newer build existing is a card the student can dismiss. Nothing about it
    is allowed to lock them out — that needs a decision, made by a person, and
    recorded as `minimum_version`.
    """
    async with client.sessions() as session:
        session.add(_release(version="1.4.0", notes="Quizzes keep your place."))
        await session.commit()

        result = await check(session, platform="android", version="1.2.0")

    assert result.update_available is True
    assert result.update_required is False
    assert result.notes == "Quizzes keep your place."


async def test_below_the_minimum_forces(client):
    async with client.sessions() as session:
        session.add(_release(version="1.4.0", minimum_version="1.3.0"))
        await session.commit()

        blocked = await check(session, platform="android", version="1.2.0")
        # On the floor exactly, not below it. This is the off-by-one that would
        # lock out the very build the floor was set to allow.
        allowed = await check(session, platform="android", version="1.3.0")

    assert blocked.update_required is True
    assert allowed.update_required is False
    assert allowed.update_available is True


async def test_the_current_build_is_told_nothing(client):
    async with client.sessions() as session:
        session.add(_release(version="1.4.0", minimum_version="1.3.0"))
        await session.commit()

        result = await check(session, platform="android", version="1.4.0")

    assert result.update_available is False
    assert result.update_required is False


async def test_an_unpublished_release_is_not_offered(client):
    """
    A build in store review exists here before anyone can install it.

    Offering it would send students to a listing that does not have it yet.
    """
    async with client.sessions() as session:
        session.add(_release(version="1.5.0", published=False))
        await session.commit()

        result = await check(session, platform="android", version="1.4.0")

    assert result.latest_version == ""
    assert result.update_available is False


async def test_the_newest_published_wins_not_the_newest_row(client):
    async with client.sessions() as session:
        session.add(_release(version="1.4.0", published=True, notes="Shipped"))
        session.add(_release(version="1.10.0", published=False, notes="In review"))
        await session.commit()

        result = await check(session, platform="android", version="1.3.0")

    assert result.latest_version == "1.4.0"
    assert result.notes == "Shipped"


async def test_platforms_do_not_leak_into_each_other(client):
    async with client.sessions() as session:
        session.add(_release(platform="android", version="1.4.0"))
        await session.commit()

        result = await check(session, platform="ios", version="1.0.0")

    assert result.update_available is False


# --- The endpoint -------------------------------------------------------------


async def test_the_check_needs_no_token(client):
    """
    Deliberately open.

    The build most likely to need forcing off the network is one that cannot
    sign in, and an update check behind a token cannot reach it.
    """
    response = await client.get("/api/v1/app/release?platform=android&version=1.0.0")

    assert response.status_code == 200
    assert response.json()["update_required"] is False


async def test_an_unknown_platform_is_answered_not_refused(client):
    """A 4xx here would be a launch-path error on every future platform."""
    response = await client.get("/api/v1/app/release?platform=web&version=1.0.0")

    assert response.status_code == 200
    assert response.json()["update_available"] is False


async def test_a_client_that_will_not_say_its_version_is_treated_as_old(client):
    async with client.sessions() as session:
        session.add(_release(version="1.4.0"))
        await session.commit()

    response = await client.get("/api/v1/app/release?platform=android")

    assert response.json()["update_available"] is True


# --- The console --------------------------------------------------------------


async def test_a_release_is_recorded_unpublished_by_default(client):
    headers = await admin_headers(client)

    created = await client.post(
        "/api/v1/admin/releases",
        json={"platform": "android", "version": "1.5.0"},
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["published"] is False
    assert created.json()["is_current"] is False


async def test_a_minimum_newer_than_the_release_is_refused(client):
    """
    Otherwise everybody is told to update to a build that is itself below the
    floor — including the one they would be updating to.
    """
    headers = await admin_headers(client)

    response = await client.post(
        "/api/v1/admin/releases",
        json={"platform": "android", "version": "1.4.0", "minimum_version": "1.5.0"},
        headers=headers,
    )

    assert response.status_code >= 400


async def test_the_same_build_cannot_be_recorded_twice(client):
    headers = await admin_headers(client)
    body = {"platform": "android", "version": "1.5.0"}

    assert (
        await client.post("/api/v1/admin/releases", json=body, headers=headers)
    ).status_code == 201
    assert (
        await client.post("/api/v1/admin/releases", json=body, headers=headers)
    ).status_code >= 400


async def test_publishing_makes_it_the_current_release(client):
    headers = await admin_headers(client)

    created = (
        await client.post(
            "/api/v1/admin/releases",
            json={"platform": "android", "version": "1.5.0"},
            headers=headers,
        )
    ).json()

    updated = await client.patch(
        f"/api/v1/admin/releases/{created['id']}",
        json={"published": True},
        headers=headers,
    )

    assert updated.json()["is_current"] is True

    # And the app now sees it.
    seen = await client.get("/api/v1/app/release?platform=android&version=1.4.0")
    assert seen.json()["latest_version"] == "1.5.0"


async def test_adoption_counts_the_devices_a_forced_update_would_strand(client):
    """
    The number that has to exist before `minimum_version` is raised.

    Without it, "force everyone off 1.3" is a decision made on a feeling rather
    than on how many people it interrupts.
    """
    student, _ = await sign_in(client)
    await client.put(
        "/api/v1/me/devices",
        json={
            "id": "11111111-1111-1111-1111-111111111111",
            "platform": "android",
            "app_version": "1.3.0",
        },
        headers=student,
    )

    headers = await admin_headers(client)
    rows = (
        await client.get("/api/v1/admin/releases/adoption", headers=headers)
    ).json()

    assert {"platform": "android", "version": "1.3.0", "devices": 1} in rows
