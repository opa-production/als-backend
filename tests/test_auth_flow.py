"""
The whole sign-in flow, against a real database.

SQLite rather than Postgres so this runs anywhere with no container. The two
things that differ — JSONB and the Postgres UUID type — are not exercised by
auth, and the alternative is a test suite nobody can run without docker up.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_http_client
from app.db.base import Base
from app.db.session import get_session
from app.main import app

PHONE = "+254712345678"


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _session

    # ASGITransport does not run the lifespan, so the shared httpx client that
    # normally lives on app.state was never created. Overriding the dependency
    # is cleaner than half-starting the lifespan: nothing under test reaches
    # the network — the console SMS provider ignores the client entirely.
    outbound = AsyncClient(base_url="http://outbound.invalid")
    app.dependency_overrides[get_http_client] = lambda: outbound

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http

    app.dependency_overrides.clear()
    await outbound.aclose()
    await engine.dispose()


async def _sign_in(client, phone=PHONE, device_id=None):
    sent = await client.post("/api/v1/auth/otp", json={"phone": phone})
    assert sent.status_code == 202
    code = sent.json()["debug_code"]
    assert code is not None, "no SMS provider configured, so the code must come back"

    return await client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": code,
            "device_id": str(device_id or uuid.uuid4()),
            "platform": "android",
        },
    )


async def test_first_sign_in_creates_the_account(client):
    response = await _sign_in(client)
    assert response.status_code == 200

    body = response.json()
    assert body["is_new_user"] is True
    assert body["access_token"] and body["refresh_token"]


async def test_second_sign_in_is_not_a_new_account(client):
    await _sign_in(client)
    again = await _sign_in(client)

    assert again.json()["is_new_user"] is False


async def test_a_wrong_code_is_refused(client):
    await client.post("/api/v1/auth/otp", json={"phone": PHONE})

    response = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": "000000"}
    )

    assert response.status_code == 401
    # The message must not reveal whether the number has an account.
    assert "code" in response.json()["message"].lower()


async def test_a_code_cannot_be_used_twice(client):
    sent = await client.post("/api/v1/auth/otp", json={"phone": PHONE})
    code = sent.json()["debug_code"]

    first = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": code}
    )
    assert first.status_code == 200

    replay = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": code}
    )
    assert replay.status_code == 401


async def test_requesting_a_new_code_kills_the_old_one(client):
    first = (await client.post("/api/v1/auth/otp", json={"phone": PHONE})).json()
    await client.post("/api/v1/auth/otp", json={"phone": PHONE})

    stale = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": first["debug_code"]}
    )
    assert stale.status_code == 401


async def test_sending_is_throttled(client):
    for _ in range(5):
        assert (
            await client.post("/api/v1/auth/otp", json={"phone": PHONE})
        ).status_code == 202

    blocked = await client.post("/api/v1/auth/otp", json={"phone": PHONE})
    assert blocked.status_code == 429


async def test_a_malformed_number_is_rejected(client):
    response = await client.post("/api/v1/auth/otp", json={"phone": "0712345678"})
    assert response.status_code == 400


async def test_the_profile_needs_a_token(client):
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_profile_round_trip(client):
    tokens = (await _sign_in(client)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    me = await client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["phone"] == PHONE
    # Every new account starts on a trial, created server-side.
    assert me.json()["subscription"]["tier"] == "trial"

    patched = await client.patch(
        "/api/v1/me", json={"full_name": "Deon", "program": "BSc CS"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["full_name"] == "Deon"

    # A patch naming one field must not blank the others.
    again = await client.patch(
        "/api/v1/me", json={"institution": "UoN"}, headers=headers
    )
    assert again.json()["full_name"] == "Deon"
    assert again.json()["program"] == "BSc CS"


async def test_refresh_rotates_and_burns_the_old_token(client):
    tokens = (await _sign_in(client)).json()

    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401


async def test_logout_revokes_the_refresh_token(client):
    tokens = (await _sign_in(client)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (
        await client.post("/api/v1/auth/logout", json={}, headers=headers)
    ).status_code == 204

    dead = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert dead.status_code == 401


async def test_deleting_the_account_locks_it_out(client):
    tokens = (await _sign_in(client)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    deleted = await client.delete("/api/v1/me", headers=headers)
    assert deleted.status_code == 200

    # The tombstone takes effect on the very next request, not at token expiry.
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 401
