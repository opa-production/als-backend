"""
The admin console.

The tests that matter here are the boundary ones — a student token must not
reach an admin route, and a support role must not grant a plan. Everything else
in this file is a shape check on responses the console will render.
"""

import uuid
from datetime import timedelta

import pytest

from app.core.clock import now as utc_now
from app.models.admin import AdminUser
from app.models.billing import Payment, Subscription
from app.services import admin_auth
from app.services.plans import Tier
from tests.conftest import sign_in

ADMIN_EMAIL = "owner@ardena.co.ke"
ADMIN_PASSWORD = "a-long-enough-password"


async def make_admin(client, *, email=ADMIN_EMAIL, role="owner"):
    """Inserts an admin directly. There is no API that creates the first one."""
    async with client.sessions() as session:
        admin = await admin_auth.create_admin(
            session,
            email=email,
            password=ADMIN_PASSWORD,
            full_name="Test Owner",
            role=role,
        )
        await session.commit()
        return admin.id


async def admin_headers(client, *, email=ADMIN_EMAIL, role="owner"):
    await make_admin(client, email=email, role=role)
    response = await client.post(
        "/api/v1/admin/auth/login",
        json={"email": email, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- The boundary ------------------------------------------------------------


async def test_admin_routes_reject_anonymous(client):
    response = await client.get("/api/v1/admin/overview")
    assert response.status_code == 401


async def test_student_token_is_not_an_admin_token(client):
    """
    The check that justifies a second token type existing.

    A student's access token is signed with the same secret and verifies
    perfectly; only ``typ`` separates them. Without that check every student in
    the system could read the revenue dashboard.
    """
    headers, _ = await sign_in(client)
    response = await client.get("/api/v1/admin/overview", headers=headers)
    assert response.status_code == 401


async def test_admin_token_is_not_a_student_token(client):
    """And the same door in the other direction."""
    headers = await admin_headers(client)
    response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401


async def test_wrong_password_and_unknown_email_look_the_same(client):
    await make_admin(client)

    wrong = await client.post(
        "/api/v1/admin/auth/login",
        json={"email": ADMIN_EMAIL, "password": "not-the-password"},
    )
    unknown = await client.post(
        "/api/v1/admin/auth/login",
        json={"email": "nobody@ardena.co.ke", "password": ADMIN_PASSWORD},
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["message"] == unknown.json()["message"]


async def test_support_cannot_grant_a_plan(client):
    """Role gating, on the endpoint that hands out money's worth of product."""
    headers = await admin_headers(client, email="support@ardena.co.ke", role="support")
    student_headers, user_id = await sign_in(client)

    response = await client.post(
        f"/api/v1/admin/users/{user_id}/subscription",
        headers=headers,
        json={"tier": "pro", "reason": "trying it on"},
    )
    assert response.status_code == 403


async def test_support_can_release_a_device_lock(client):
    """The one privileged action support does need, since it grants nothing."""
    headers = await admin_headers(client, email="support@ardena.co.ke", role="support")
    _, user_id = await sign_in(client)

    response = await client.post(
        f"/api/v1/admin/users/{user_id}/device-reset", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


# --- Sessions ----------------------------------------------------------------


async def test_refresh_rotates_and_burns_the_old_token(client):
    await make_admin(client)
    tokens = (
        await client.post(
            "/api/v1/admin/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
    ).json()

    rotated = await client.post(
        "/api/v1/admin/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]

    replayed = await client.post(
        "/api/v1/admin/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replayed.status_code == 401


async def test_deactivating_an_admin_ends_their_session_now(client):
    """
    Not at the next login — now.

    An access token lasts an hour. If deactivation only stopped the next sign
    in, a compromised account would stay live for exactly the hour that
    matters.
    """
    owner = await admin_headers(client)
    other_id = await make_admin(client, email="other@ardena.co.ke", role="admin")

    other = (
        await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "other@ardena.co.ke", "password": ADMIN_PASSWORD},
        )
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    assert (await client.get("/api/v1/admin/auth/me", headers=other_headers)).status_code == 200

    deleted = await client.delete(
        f"/api/v1/admin/admins/{other_id}?reason=left+the+team", headers=owner
    )
    assert deleted.status_code == 200

    assert (await client.get("/api/v1/admin/auth/me", headers=other_headers)).status_code == 401


async def test_last_owner_cannot_be_removed(client):
    headers = await admin_headers(client)
    me = (await client.get("/api/v1/admin/auth/me", headers=headers)).json()

    # Removing yourself is refused first, so promote a second admin and try to
    # strip the only owner role instead.
    second = await make_admin(client, email="second@ardena.co.ke", role="admin")
    response = await client.patch(
        f"/api/v1/admin/admins/{me['id']}", headers=headers, json={"role": "admin"}
    )
    assert response.status_code == 400
    assert "last active owner" in response.json()["message"]
    assert second is not None


# --- Dashboard ---------------------------------------------------------------


async def test_overview_shape(client):
    headers = await admin_headers(client)
    await sign_in(client)

    response = await client.get("/api/v1/admin/overview", headers=headers)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["users"]["total"] == 1
    assert body["revenue"]["currency"] == "KES"
    assert body["revenue"]["gross_ksh"] == 0
    # Trial plus every sellable plan — each of the three families monthly and
    # as a Season.
    assert {row["tier"] for row in body["plans"]} == {
        "trial",
        "standard",
        "pro",
        "friends",
        "standard_season",
        "pro_season",
        "friends_season",
    }
    assert body["funnel"]["signed_up"] == 1


async def test_timeseries_fills_empty_days(client):
    """
    The gap-filling is the behaviour under test.

    A chart that omits days with no rows draws a straight line through the
    outage that caused them.
    """
    headers = await admin_headers(client)
    await sign_in(client)

    response = await client.get(
        "/api/v1/admin/overview/timeseries?metric=signups&days=7", headers=headers
    )
    assert response.status_code == 200

    body = response.json()
    assert len(body["points"]) == 7
    assert body["total"] == 1


async def test_unknown_metric_is_a_readable_error(client):
    headers = await admin_headers(client)
    response = await client.get(
        "/api/v1/admin/overview/timeseries?metric=vibes", headers=headers
    )
    assert response.status_code == 400
    assert "signups" in response.json()["message"]


# --- Revenue -----------------------------------------------------------------


@pytest.fixture
async def paid_student(client):
    """A student on Synapse with one successful payment behind it."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.user_id == user_id
            )
        )
        subscription.tier = Tier.PRO.value
        subscription.verified = True
        subscription.expires_at = utc_now() + timedelta(days=30)

        session.add(
            Payment(
                user_id=user_id,
                reference=f"ref-{uuid.uuid4().hex[:10]}",
                tier=Tier.PRO.value,
                amount_kes=350,
                status="success",
                channel="mobile_money",
                paid_at=utc_now(),
            )
        )
        await session.commit()

    return user_id


async def test_revenue_summary_counts_only_successful_payments(client, paid_student):
    headers = await admin_headers(client)

    async with client.sessions() as session:
        session.add(
            Payment(
                user_id=paid_student,
                reference="ref-failed",
                tier=Tier.PRO.value,
                amount_kes=350,
                status="failed",
            )
        )
        await session.commit()

    response = await client.get("/api/v1/admin/revenue/summary", headers=headers)
    assert response.status_code == 200

    body = response.json()
    assert body["gross_ksh"] == 350
    assert body["successful_payments"] == 1
    assert body["failed_payments"] == 1
    assert body["success_rate_pct"] == 50.0
    assert body["by_channel"] == {"mobile_money": 350}


async def test_mrr_counts_a_friends_group_once_not_five_times(client):
    """
    The Friends plan is the one place a naive sum is wrong by 5x.

    One payment of KES 1,250 creates up to five subscriptions on ``friends``.
    Summing the plan price per subscriber would report 6,250 of recurring
    revenue that does not exist.
    """
    from app.services import billing as billing_service

    headers = await admin_headers(client)
    _, owner_id = await sign_in(client, phone="+254700000001")
    _, joiner_id = await sign_in(client, phone="+254700000002")

    async with client.sessions() as session:
        await billing_service.activate(
            session, user_id=owner_id, tier=Tier.FRIENDS, verified=True
        )
        group = await billing_service.open_group(session, owner_id=owner_id)
        await billing_service.join_group(
            session, user_id=joiner_id, code=group.invite_code
        )
        await session.commit()

    response = await client.get("/api/v1/admin/revenue/by-plan", headers=headers)
    friends = next(row for row in response.json() if row["tier"] == "friends")

    assert friends["active"] == 2, "two people hold a seat"
    assert friends["mrr_ksh"] == 1250, "one group, one payment's worth of MRR"


async def test_paying_excludes_trials(client):
    headers = await admin_headers(client)
    await sign_in(client)

    response = await client.get("/api/v1/admin/subscriptions/stats", headers=headers)
    body = response.json()

    assert body["total_free"] == 1
    assert body["total_trial"] == 0
    assert body["total_paying"] == 0
    assert body["mrr_ksh"] == 0


# --- Users -------------------------------------------------------------------


async def test_user_search_and_detail(client, paid_student):
    headers = await admin_headers(client)

    listed = await client.get("/api/v1/admin/users?status=paying", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["tier"] == "pro"
    assert body["items"][0]["total_paid_ksh"] == 350

    detail = await client.get(f"/api/v1/admin/users/{paid_student}", headers=headers)
    assert detail.status_code == 200
    detailed = detail.json()
    assert detailed["effective_tier"] == "pro"
    assert detailed["total_paid_ksh"] == 350
    assert len(detailed["payments"]) == 1
    assert "monthly_ai_queries" in detailed["limits"]


async def test_detail_shows_when_entitlement_disagrees_with_the_row(client):
    """
    The screen support actually needs.

    An unverified paid subscription looks live in the ``subscriptions`` row and
    resolves to ``expired`` everywhere it counts. Showing both side by side is
    what turns "the app says I have Synapse" into an answerable ticket.
    """
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            __import__("sqlalchemy").select(Subscription).where(
                Subscription.user_id == user_id
            )
        )
        subscription.tier = Tier.PRO.value
        subscription.verified = False
        subscription.expires_at = utc_now() + timedelta(days=30)
        await session.commit()

    detail = (
        await client.get(f"/api/v1/admin/users/{user_id}", headers=headers)
    ).json()

    assert detail["subscription"]["tier"] == "pro"
    assert detail["subscription"]["is_active"] is False
    assert detail["effective_tier"] == "free"


async def test_granting_a_plan_writes_an_audit_entry(client):
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    granted = await client.post(
        f"/api/v1/admin/users/{user_id}/subscription",
        headers=headers,
        json={"tier": "pro", "days": 30, "reason": "compensation for the outage"},
    )
    assert granted.status_code == 200
    assert granted.json()["tier"] == "pro"
    assert granted.json()["is_active"] is True

    entries = (
        await client.get(f"/api/v1/admin/audit?target_id={user_id}", headers=headers)
    ).json()

    assert entries["total"] == 1
    entry = entries["items"][0]
    assert entry["action"] == "subscription.granted"
    assert "compensation for the outage" in entry["summary"]
    assert entry["meta"]["before"]["tier"] == "free"


async def test_grant_extends_rather_than_restarts_the_same_tier(client):
    """A goodwill week on top of ten days left is seventeen, not seven."""
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    first = await client.post(
        f"/api/v1/admin/users/{user_id}/subscription",
        headers=headers,
        json={"tier": "pro", "days": 10, "reason": "first grant"},
    )
    second = await client.post(
        f"/api/v1/admin/users/{user_id}/subscription",
        headers=headers,
        json={"tier": "pro", "days": 7, "extend": True, "reason": "goodwill"},
    )

    assert first.json()["days_remaining"] in (9, 10)
    assert second.json()["days_remaining"] in (16, 17)


async def test_grant_requires_a_reason(client):
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    response = await client.post(
        f"/api/v1/admin/users/{user_id}/subscription",
        headers=headers,
        json={"tier": "pro"},
    )
    assert response.status_code == 422


async def test_device_reset_lets_a_replaced_phone_sign_in(client):
    """
    The most common support ticket this product will get.

    One device per account is enforced on every request, so a lost phone locks
    a paying student out with no self-service way back.
    """
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        from app.models.account import User

        user = await session.get(User, user_id)
        assert user.active_device_id is not None
        await session.commit()

    response = await client.post(
        f"/api/v1/admin/users/{user_id}/device-reset?reason=lost+phone", headers=headers
    )
    assert response.status_code == 200

    async with client.sessions() as session:
        from app.models.account import User

        user = await session.get(User, user_id)
        assert user.active_device_id is None


async def test_delete_is_a_tombstone_not_a_hard_delete(client):
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    deleted = await client.delete(
        f"/api/v1/admin/users/{user_id}?reason=requested+by+the+student",
        headers=headers,
    )
    assert deleted.status_code == 200

    listed = (await client.get("/api/v1/admin/users", headers=headers)).json()
    assert listed["total"] == 0

    tombstones = (
        await client.get("/api/v1/admin/users?status=deleted", headers=headers)
    ).json()
    assert tombstones["total"] == 1
    assert tombstones["items"][0]["is_deleted"] is True

    restored = await client.post(
        f"/api/v1/admin/users/{user_id}/restore?reason=mistake", headers=headers
    )
    assert restored.status_code == 200
    assert (await client.get("/api/v1/admin/users", headers=headers)).json()["total"] == 1


async def test_usage_reset_clears_counters(client):
    headers = await admin_headers(client)
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        from app.services.quota import record_usage

        await record_usage(session, user_id, "ai_queries", 5)
        await session.commit()

    before = (
        await client.get(f"/api/v1/admin/users/{user_id}/usage", headers=headers)
    ).json()
    assert before["counters"]["ai_queries"]

    cleared = await client.post(
        f"/api/v1/admin/users/{user_id}/usage/reset",
        headers=headers,
        json={"metric": "ai_queries", "reason": "our fault"},
    )
    assert cleared.status_code == 200

    after = (
        await client.get(f"/api/v1/admin/users/{user_id}/usage", headers=headers)
    ).json()
    assert after["counters"] == {}


# --- Ops ---------------------------------------------------------------------


async def test_ops_health_reports_unconfigured_integrations(client):
    """
    The row that stops someone reading an empty dashboard as a dead business.
    """
    headers = await admin_headers(client)

    response = await client.get("/api/v1/admin/ops/health", headers=headers)
    assert response.status_code == 200

    body = response.json()
    assert body["database_ok"] is True
    assert set(body["integrations"]) == {
        "kora",
        "supabase_storage",
        "sms",
        "google_sign_in",
    }
    assert any("credentials" in warning for warning in body["warnings"])


async def test_ops_plans_is_the_server_side_catalogue(client):
    headers = await admin_headers(client)
    response = await client.get("/api/v1/admin/ops/plans", headers=headers)

    plans = {row["id"]: row for row in response.json()}
    assert plans["pro"]["price_ksh"] == 350
    assert plans["friends"]["price_per_seat_ksh"] == 208
    assert plans["free"]["limits"]["monthly_ai_queries"] == 30


# --- Audit -------------------------------------------------------------------


async def test_audit_records_the_login_itself(client):
    headers = await admin_headers(client)

    entries = (
        await client.get("/api/v1/admin/audit?action=admin.signed_in", headers=headers)
    ).json()

    assert entries["total"] == 1
    assert entries["items"][0]["admin_email"] == ADMIN_EMAIL


async def test_support_can_read_the_audit_log(client):
    """A log only the people who can edit it may read is not a check on them."""
    await make_admin(client)
    headers = await admin_headers(client, email="support@ardena.co.ke", role="support")

    response = await client.get("/api/v1/admin/audit", headers=headers)
    assert response.status_code == 200


async def test_admin_row_never_leaks_a_password_hash(client):
    headers = await admin_headers(client)

    listed = await client.get("/api/v1/admin/admins", headers=headers)
    assert listed.status_code == 200
    assert "password_hash" not in listed.text
    assert AdminUser.__tablename__ == "admin_users"
