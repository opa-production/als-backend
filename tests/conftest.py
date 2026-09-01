"""
Shared fixtures.

SQLite in memory rather than Postgres so the suite runs anywhere with no
container. The two Postgres-only types are declared as portable variants in
``app/db/base.py`` for exactly this reason.
"""

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_http_client
from app.core.config import settings
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


async def give_plan(client, user_id, tier):
    """
    Puts an account on a paid plan.

    Free allows one course unit, which is deliberate and is the wrong subject
    for a test about something else -- a sync cursor, say. This keeps the cap
    out of the way of tests that are not about the cap.
    """
    from app.services.billing import activate

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=tier, verified=True)
        await session.commit()


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


# --- Payment providers --------------------------------------------------------


class _FakeKora:
    """
    Stands in for Kora and remembers what it was sent.

    What matters in these tests is the *request* — the amount and the metadata
    are what a wrong checkout gets wrong, and both are decided on this side
    rather than by Kora.
    """

    def __init__(self):
        self.payload = None
        #: What a later verify should report. Tests that only exercise checkout
        #: never look at it.
        self.verify_status = "success"
        self.verify_amount = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Verifying a reference is a GET with no body; creating a charge is a
        # POST with one. Branching on the method keeps both in one fake.
        if request.method == "GET":
            reference = str(request.url).rstrip("/").rsplit("/", 1)[-1]
            amount = self.verify_amount
            if amount is None:
                amount = (self.payload or {}).get("amount", 0)
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Charge retrieved",
                    "data": {
                        "reference": reference,
                        "status": self.verify_status,
                        "amount": amount,
                        "currency": "KES",
                        "payment_method": "mobile_money",
                        "metadata": (self.payload or {}).get("metadata", {}),
                    },
                },
            )

        self.payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Charge created successfully",
                "data": {
                    "checkout_url": "https://checkout.korapay.com/abc123/pay",
                    "reference": self.payload["reference"],
                },
            },
        )


@pytest.fixture
def kora(client, monkeypatch):
    """
    Swaps the *outbound* client only.

    Patching ``httpx.AsyncClient.post`` would also intercept the test client's
    own requests into the app, since both are httpx — so the seam is the
    dependency, not the library.
    """
    monkeypatch.setattr(settings, "kora_secret_key", "sk_test_x")
    fake = _FakeKora()

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    app.dependency_overrides[get_http_client] = lambda: outbound

    return fake
