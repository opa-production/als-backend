"""
Shared fixtures.

SQLite in memory rather than Postgres so the suite runs anywhere with no
container. The two Postgres-only types are declared as portable variants in
``app/db/base.py`` for exactly this reason.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_http_client
from app.db.base import Base
from app.db.session import get_session
from app.main import app

PHONE = "+254712345678"
OTHER_PHONE = "+254711111111"


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # SQLite ignores foreign keys unless asked. Without this the harness lets
    # through exactly the deletes Postgres would cascade, so an ON DELETE rule
    # could be wrong for months and every test would still pass.
    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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
    # beats half-starting the lifespan: nothing under test reaches the network.
    outbound = AsyncClient(base_url="http://outbound.invalid")
    app.dependency_overrides[get_http_client] = lambda: outbound

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        http.sessions = sessions
        yield http

    app.dependency_overrides.clear()
    await outbound.aclose()
    await engine.dispose()


async def sign_in(client, phone=PHONE):
    """Signs a student in and returns ready-to-use auth headers."""
    sent = await client.post("/api/v1/auth/otp", json={"phone": phone})
    code = sent.json()["debug_code"]

    tokens = (
        await client.post(
            "/api/v1/auth/otp/verify",
            json={"phone": phone, "code": code, "device_id": str(uuid.uuid4())},
        )
    ).json()

    return {"Authorization": f"Bearer {tokens['access_token']}"}, uuid.UUID(
        tokens["user_id"]
    )
