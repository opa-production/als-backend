"""
Billing: the parts where being wrong costs money.

Webhook signatures, replay, seat accounting and what happens when a plan runs
out. Everything here is either a way to give the product away or a way to
charge someone twice.
"""

import hashlib
import hmac
import json

import pytest

from app.core.clock import now as utc_now
from app.core.config import settings
from app.services.billing import activate, tier_from_charge
from app.services.paystack import Charge, verify_signature
from app.services.plans import Tier, plan_for
from tests.conftest import OTHER_PHONE, sign_in

SECRET = "test-webhook-secret"


def _charge(amount_kes: int, *, tier: str | None = None, reference="ref_1") -> Charge:
    return Charge(
        reference=reference,
        status="success",
        amount_kes=amount_kes,
        channel="mobile_money",
        email="student@example.com",
        metadata={"tier": tier} if tier else {},
    )


def _signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
    return raw, signature


# --- Signature ---------------------------------------------------------------


def test_a_forged_webhook_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "paystack_webhook_secret", SECRET)
    raw, _ = _signed({"event": "charge.success"})

    assert verify_signature(raw, "not-the-signature") is False
    assert verify_signature(raw, None) is False


def test_a_genuine_webhook_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "paystack_webhook_secret", SECRET)
    raw, signature = _signed({"event": "charge.success"})

    assert verify_signature(raw, signature) is True


def test_signature_is_over_the_raw_body(monkeypatch):
    """
    Re-serialising the parsed JSON changes whitespace, and the digest no longer
    matches. This is why the handler reads `request.body()` rather than the
    parsed payload.
    """
    monkeypatch.setattr(settings, "paystack_webhook_secret", SECRET)
    body = {"event": "charge.success", "data": {"reference": "r"}}
    raw, signature = _signed(body)

    reserialised = json.dumps(body, indent=2).encode()
    assert verify_signature(reserialised, signature) is False


def test_no_secret_means_nothing_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "paystack_webhook_secret", "")
    raw, signature = _signed({"event": "charge.success"})

    assert verify_signature(raw, signature) is False


# --- Which plan was paid for --------------------------------------------------


def test_metadata_alone_cannot_buy_a_plan():
    """
    Metadata is a field on a checkout we do not fully control. Trusting it
    would mean a KES 150 payment tagged `tier=pro` buys Synapse — the amount
    is the fact, and metadata only narrows it.
    """
    assert tier_from_charge(_charge(150, tier="pro")) is Tier.STANDARD
    assert tier_from_charge(_charge(350, tier="pro")) is Tier.PRO


def test_metadata_cannot_name_a_tier_that_is_not_for_sale():
    # "expired" is a real Tier and worth nothing. It must never resolve.
    assert tier_from_charge(_charge(350, tier="expired")) is Tier.PRO


def test_the_amount_resolves_the_tier_without_metadata():
    assert tier_from_charge(_charge(150)) is Tier.STANDARD
    assert tier_from_charge(_charge(350)) is Tier.PRO
    assert tier_from_charge(_charge(1250)) is Tier.FRIENDS


def test_paying_between_two_plans_gets_the_lower_one():
    """
    KES 1000 is more than Synapse and less than Friends. It buys Synapse —
    resolving upward would hand out five seats for four seats' money.
    """
    assert tier_from_charge(_charge(1000)) is Tier.PRO


def test_friends_is_cheaper_per_head_than_synapse_alone():
    """The entire proposition. If this ever inverts, the plan is pointless."""
    friends = plan_for(Tier.FRIENDS)
    assert friends.price_per_seat_ksh == 250
    assert friends.price_per_seat_ksh < plan_for(Tier.PRO).price_ksh


def test_an_amount_below_every_plan_is_refused():
    from app.core.errors import AppError

    with pytest.raises(AppError):
        tier_from_charge(_charge(20))


# --- Subscriptions ------------------------------------------------------------


async def test_renewing_extends_rather_than_restarts(client):
    """Paying a week early must not throw that week away."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        first = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        first_end = first.expires_at
        await session.commit()

    async with client.sessions() as session:
        second = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        await session.commit()

    days = plan_for(Tier.PRO).duration_days
    assert (second.expires_at - first_end).days == pytest.approx(days, abs=1)


async def test_switching_tier_starts_a_fresh_period(client):
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=Tier.STANDARD, verified=True)
        switched = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        await session.commit()

    days = plan_for(Tier.PRO).duration_days
    assert (switched.expires_at - utc_now()).days == pytest.approx(days, abs=1)


async def test_subscription_endpoint_reports_the_trial(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()
    assert body["tier"] == "trial"
    assert body["days_remaining"] <= 14
    assert body["days_remaining"] >= 13
    assert body["is_expired"] is False


async def test_a_lapsed_plan_reports_as_expired(client):
    """
    Not "trial". Falling back to trial limits would be a free tier nobody
    agreed to sell: pay for one month, lapse, keep the trial forever.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        from sqlalchemy import select

        from app.models.billing import Subscription

        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = Tier.PRO.value
        subscription.expires_at = utc_now().replace(year=2020)
        await session.commit()

    headers, _ = await sign_in(client)
    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()
    assert body["tier"] == "expired"
    assert body["is_expired"] is True
    # What they had is still reported, so the app can name what ended.
    assert body["nominal_tier"] == "pro"


async def test_only_sellable_plans_are_advertised(client):
    body = (await client.get("/api/v1/billing/plans")).json()

    ids = {plan["id"] for plan in body}
    assert ids == {"standard", "pro", "friends"}
    # Neither the trial nor the expired tier is a product.
    assert "trial" not in ids
    assert "expired" not in ids

    friends = next(p for p in body if p["id"] == "friends")
    assert friends["seats"] == 5
    assert friends["price_per_seat_ksh"] == 250


# --- Friends ------------------------------------------------------------------


async def test_a_group_needs_a_friends_plan_first(client):
    """Otherwise a group is five free Synapse seats."""
    headers, _ = await sign_in(client)

    assert (await client.post("/api/v1/billing/group", headers=headers)).status_code == 403


async def _friends_owner(client):
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=Tier.FRIENDS, verified=True)
        await session.commit()

    return headers, user_id


async def test_the_owner_holds_one_of_the_five_seats(client):
    headers, _ = await _friends_owner(client)

    group = (await client.post("/api/v1/billing/group", headers=headers)).json()
    assert group["seats"] == 5
    assert group["seats_taken"] == 1


async def test_a_friend_can_join_and_gets_the_entitlement(client):
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    joined = await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    assert joined.status_code == 200
    assert joined.json()["seats_taken"] == 2

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()
    assert subscription["tier"] == "friends"


async def test_joining_twice_is_not_an_error(client):
    """Tapping an invite link twice is not a mistake worth a message."""
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )
    again = await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    assert again.status_code == 200
    assert again.json()["seats_taken"] == 2


async def test_a_bad_invite_code_is_refused(client):
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/billing/group/join", json={"code": "NOPE1234"}, headers=headers
    )
    assert response.status_code == 404


async def test_the_owner_cannot_be_removed(client):
    """The seat would be unreclaimable and the group would outlive the payer."""
    headers, owner_id = await _friends_owner(client)
    await client.post("/api/v1/billing/group", headers=headers)

    response = await client.delete(
        f"/api/v1/billing/group/members/{owner_id}", headers=headers
    )
    assert response.status_code == 400


async def test_removing_a_member_frees_the_seat_and_the_entitlement(client):
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, friend_id = await sign_in(client, phone=OTHER_PHONE)
    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    removed = await client.delete(
        f"/api/v1/billing/group/members/{friend_id}", headers=owner_headers
    )
    assert removed.status_code == 204

    members = (
        await client.get("/api/v1/billing/group/members", headers=owner_headers)
    ).json()
    assert len(members) == 1

    # The entitlement came from the group and goes back with the seat — to
    # expired, never to a fresh trial.
    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()
    assert subscription["tier"] == "expired"


async def test_someone_elses_group_is_not_yours_to_manage(client):
    owner_headers, _ = await _friends_owner(client)
    await client.post("/api/v1/billing/group", headers=owner_headers)

    stranger_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    assert (
        await client.get("/api/v1/billing/group", headers=stranger_headers)
    ).status_code == 404
