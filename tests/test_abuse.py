"""
The rules that stop the plans being free.

Every test here describes an attack someone will actually try. If one of these
goes red, the product is being given away — so each says what the attack is,
not just what the assertion is.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.models.account import User
from app.models.billing import Subscription
from app.models.trial import TrialGrant
from app.services.billing import activate, assert_charge_belongs_to
from app.services.plans import Tier, plan_for
from app.services.quota import check_ai_query, get_entitlement
from tests.conftest import OTHER_PHONE, PHONE, sign_in
from tests.test_billing import _charge

# --- One trial per person, for good -------------------------------------------


async def test_a_new_account_gets_fourteen_days(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()
    assert body["tier"] == "trial"
    assert body["days_remaining"] >= 13


async def test_deleting_and_signing_back_in_does_not_reset_the_trial(client):
    """
    The attack: burn the fortnight, delete the account, sign up on the same
    number, repeat forever.

    Inside the deletion window the account comes back as it was — same trial,
    same end date, not a new one. That is the protection: the clock does not
    restart.
    """
    headers, _ = await sign_in(client)
    before = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()

    assert (await client.delete("/api/v1/me", headers=headers)).status_code == 200

    again, _ = await sign_in(client)
    after = (await client.get("/api/v1/billing/subscription", headers=again)).json()

    # The same fortnight, not a fresh one.
    assert after["expires_at"] == before["expires_at"]

    async with client.sessions() as session:
        grants = (await session.scalars(select(TrialGrant))).all()
    assert len(grants) == 1


async def test_an_exhausted_trial_stays_exhausted_after_a_delete(client):
    """
    The same attack, run to completion: wait out the fortnight *then* delete
    and come back. This is the one that has to hold.
    """
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.expires_at = utc_now() - timedelta(days=1)
        await session.commit()

    await client.delete("/api/v1/me", headers=headers)

    again, _ = await sign_in(client)
    body = (await client.get("/api/v1/billing/subscription", headers=again)).json()

    assert body["tier"] == "expired"
    assert body["is_expired"] is True


async def test_a_hard_deleted_account_still_cannot_get_a_second_trial(client):
    """
    Once the retention sweep has removed the row, signing up is a genuinely
    new account — and the grant, which outlives the user, is what refuses it.
    """
    headers, user_id = await sign_in(client)
    await client.delete("/api/v1/me", headers=headers)

    # What the retention sweep will eventually do.
    async with client.sessions() as session:
        user = await session.get(User, user_id)
        await session.delete(user)
        await session.commit()

    again, new_user_id = await sign_in(client)
    assert new_user_id != user_id

    body = (await client.get("/api/v1/billing/subscription", headers=again)).json()
    assert body["tier"] == "expired"


async def test_the_trial_record_survives_the_account(client):
    """
    A grant hanging off the user row would vanish with the user row — which is
    precisely the moment it has to still be true.
    """
    headers, user_id = await sign_in(client)
    await client.delete("/api/v1/me", headers=headers)

    async with client.sessions() as session:
        grants = (await session.scalars(select(TrialGrant))).all()

    assert len(grants) >= 1
    assert grants[0].granted_to_user_id == user_id


async def test_the_stored_identity_is_not_the_phone_number(client):
    """A table that is never deleted must not be a list of everyone's number."""
    await sign_in(client)

    async with client.sessions() as session:
        grant = await session.scalar(select(TrialGrant))

    assert PHONE not in grant.identity_hash
    assert grant.identity_hash != PHONE
    assert len(grant.identity_hash) == 64


async def test_a_second_number_still_gets_its_own_trial(client):
    """
    The defence must not catch innocents: a genuinely different student is a
    different identity and gets their fourteen days.
    """
    await sign_in(client)
    other, _ = await sign_in(client, phone=OTHER_PHONE)

    body = (await client.get("/api/v1/billing/subscription", headers=other)).json()
    assert body["tier"] == "trial"


# --- Expiry bites immediately -------------------------------------------------


async def test_an_expired_trial_blocks_metered_features(client):
    """
    "Restrictions enforced immediately" means on the very next request, not
    after a nightly job has caught up.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        assert entitlement.tier is Tier.EXPIRED

        with pytest.raises(AppError):
            await check_ai_query(session, user_id, entitlement)


async def test_an_expired_account_can_still_read_its_own_work(client):
    """
    Locking someone out of notes they wrote is hostage-taking, not billing.
    Reads and sync stay open at every tier.
    """
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.expires_at = utc_now() - timedelta(days=1)
        await session.commit()

    assert (await client.get("/api/v1/me", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/sync", headers=headers)).status_code == 200
    assert (await client.delete("/api/v1/me", headers=headers)).status_code == 200


async def test_an_unverified_paid_plan_grants_nothing(client):
    """
    The app writes a subscription optimistically when a student says they
    paid. Until Kora confirms it, that is a claim — and a claim that
    unlocked the product would make the payment optional.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = Tier.PRO.value
        subscription.expires_at = utc_now() + timedelta(days=30)
        subscription.verified = False
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    assert entitlement.tier is Tier.EXPIRED
    assert entitlement.nominal_tier is Tier.PRO


async def test_a_tampered_tier_is_worth_nothing(client):
    """A junk value in the column must resolve down, never up."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = "unlimited_god_mode"
        subscription.expires_at = utc_now() + timedelta(days=999)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    assert entitlement.tier is Tier.EXPIRED


# --- Payments belong to the person who made them ------------------------------


def test_a_reference_from_another_account_is_refused():
    """
    References travel — receipts, screenshots, support threads. Without this,
    /billing/verify is a free plan for anyone who can obtain one.
    """
    charge = _charge(350, reference="ref_x")
    charge = type(charge)(**{**charge.__dict__, "metadata": {"user_id": str(uuid.uuid4())}})

    with pytest.raises(AppError):
        assert_charge_belongs_to(charge, user_id=uuid.uuid4(), email=None)


def test_a_payment_matching_the_caller_is_accepted():
    mine = uuid.uuid4()
    charge = _charge(350)
    charge = type(charge)(**{**charge.__dict__, "metadata": {"user_id": str(mine)}})

    assert_charge_belongs_to(charge, user_id=mine, email=None)


def test_email_matches_when_checkout_carried_no_metadata():
    charge = _charge(350)  # email is student@example.com

    assert_charge_belongs_to(
        charge, user_id=uuid.uuid4(), email="Student@Example.com "
    )


def test_an_unattributable_payment_is_refused():
    """Better a support ticket than handing a plan to whoever asked first."""
    charge = _charge(350)

    with pytest.raises(AppError):
        assert_charge_belongs_to(
            charge, user_id=uuid.uuid4(), email="someone.else@example.com"
        )


# --- One device at a time -----------------------------------------------------


async def test_signing_in_elsewhere_kills_the_first_session(client):
    """A plan bought for one student must not cover a whole hostel."""
    first, _ = await sign_in(client)
    assert (await client.get("/api/v1/me", headers=first)).status_code == 200

    second, _ = await sign_in(client)  # same number, new device id

    assert (await client.get("/api/v1/me", headers=second)).status_code == 200
    # Immediately, not when the old access token expires.
    assert (await client.get("/api/v1/me", headers=first)).status_code == 401


async def test_the_old_devices_refresh_token_is_dead_too(client):
    first_headers, _ = await sign_in(client)

    sent = await client.post("/api/v1/auth/otp", json={"phone": PHONE})
    old_refresh = None

    # Grab a refresh token for the first device, then sign in on a second.
    tokens = (
        await client.post(
            "/api/v1/auth/otp/verify",
            json={
                "phone": PHONE,
                "code": sent.json()["debug_code"],
                "device_id": str(uuid.uuid4()),
            },
        )
    ).json()
    old_refresh = tokens["refresh_token"]

    await sign_in(client)  # a third device takes over

    dead = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    )
    assert dead.status_code == 401


async def test_the_active_device_is_recorded(client):
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        user = await session.get(User, user_id)

    assert user.active_device_id is not None


# --- Group seats --------------------------------------------------------------


async def test_joining_a_group_does_not_trample_a_plan_you_paid_for(client):
    """
    Someone with their own Synapse who joins a friend's group should not have
    it silently replaced by a seat that expires sooner.
    """
    owner_headers, owner_id = await sign_in(client)
    async with client.sessions() as session:
        await activate(session, user_id=owner_id, tier=Tier.FRIENDS, verified=True)
        await session.commit()

    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, friend_id = await sign_in(client, phone=OTHER_PHONE)
    async with client.sessions() as session:
        await activate(session, user_id=friend_id, tier=Tier.PRO, verified=True)
        await session.commit()

    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    body = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()
    assert body["tier"] == "pro"
    assert body["days_remaining"] >= plan_for(Tier.PRO).duration_days - 1
