"""
The rules that stop the plans being free.

Every test here describes an attack someone will actually try. If one of these
goes red, the product is being given away — so each says what the attack is,
not just what the assertion is.
"""

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.models.account import User
from app.models.billing import Subscription
from app.models.settings import UserSettings
from app.services.billing import activate, assert_charge_belongs_to
from app.services.plans import UNIT_HARD_CAP, UNLIMITED, Tier, limits_for, plan_for, unit_cap
from app.services.quota import (
    METRIC_PERIODS,
    check_ai_query,
    get_entitlement,
    record_usage,
    resets_on,
    user_zone,
)
from tests.conftest import OTHER_PHONE, PHONE, give_plan, sign_in
from tests.test_billing import _charge

# --- The free floor -----------------------------------------------------------
#
# There used to be a fortnight's trial here, and most of this file was about
# defending it: one grant per identity, a keyed hash of every phone number, and
# a rule for what a student got when they deleted the account and came back.
#
# A free tier that never ends has nothing worth stealing, so none of that is
# needed. What replaces it is smaller and more important -- that free is small
# enough to be a demonstration rather than a product, and that nothing resolves
# *upward* from it by accident.


async def test_a_new_account_is_on_the_free_plan(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()

    assert body["tier"] == "free"
    # Free does not run out. A countdown here would be a paywall with no date.
    assert body["expires_at"] is None


async def test_deleting_and_signing_back_in_gains_nothing(client):
    """
    The attack the trial ledger existed to stop: burn the allowance, delete the
    account, sign up again on the same number, repeat.

    It now gains nothing, because there is nothing better to come back to. This
    is the test that says the removal was safe.
    """
    headers, _ = await sign_in(client)
    assert (await client.delete("/api/v1/me", headers=headers)).status_code == 200

    again, _ = await sign_in(client)
    body = (await client.get("/api/v1/billing/subscription", headers=again)).json()

    assert body["tier"] == "free"


async def test_free_is_a_demonstration_not_a_product(client):
    """
    Thirty questions a month and a hundred pages that never come back. Enough
    to see whether the tutor answers from your own notes; not enough to revise
    a semester on.

    Units are deliberately absent. A free student files their whole timetable
    like anyone else -- the demonstration is bounded by what costs us money,
    which is pages extracted and questions answered.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    assert entitlement.tier is Tier.FREE
    assert entitlement.limits.monthly_ai_queries == 30
    assert entitlement.limits.total_pdf_pages_pool == 100
    # Both pool figures are the same on Free, which is what makes the monthly
    # meter behave as the lifetime allowance it is: the month cannot refill
    # past the total.
    assert entitlement.limits.lifetime_pdf_pages == 100


async def test_units_are_not_a_thing_a_plan_buys(client):
    """
    The same ceiling everywhere, Free included.

    This is the regression guard on the change: the moment `unit_cap` starts
    varying by tier again, a free student is refused half-way through building
    their own timetable, before the app has shown them anything.
    """
    for tier in Tier:
        assert unit_cap() == UNIT_HARD_CAP, tier


async def test_a_paid_page_pool_refills(client):
    """
    The pool is a rate on a paid plan, not a wall.

    It was lifetime for every tier, which meant a student who kept paying
    eventually could not upload anything -- and got no reset date to explain
    it, because a lifetime meter has none.
    """
    assert METRIC_PERIODS["pdf_pages"] is not METRIC_PERIODS["pdf_pages_lifetime"]
    assert resets_on("pdf_pages") is not None
    assert resets_on("pdf_pages_lifetime") is None

    assert limits_for(Tier.PRO).lifetime_pdf_pages == UNLIMITED
    assert limits_for(Tier.STANDARD).lifetime_pdf_pages == UNLIMITED


async def test_the_free_monthly_allowance_actually_refuses(client):
    """A limit that is advertised and not enforced is not a limit."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

        for _ in range(entitlement.limits.monthly_ai_queries):
            await check_ai_query(session, user_id, entitlement)
            await record_usage(session, user_id, "ai_queries")

        with pytest.raises(AppError):
            await check_ai_query(session, user_id, entitlement)


async def test_a_months_allowance_is_spendable_in_one_night(client):
    """
    The reason the daily cap went.

    Revision happens the night before a CAT, not in even daily portions. A
    student with a month's questions left must be able to spend all of them in
    one sitting -- it costs no more than spending them across thirty days, and
    refusing is refusing at the only moment the app mattered.
    """
    _, user_id = await sign_in(client)
    await give_plan(client, user_id, Tier.STANDARD)

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        allowance = entitlement.limits.monthly_ai_queries
        assert allowance == 400

        # One sitting, no clock advanced between them.
        for _ in range(allowance):
            await check_ai_query(session, user_id, entitlement)
            await record_usage(session, user_id, "ai_queries")

        with pytest.raises(AppError) as refused:
            await check_ai_query(session, user_id, entitlement)

    assert "this month" in str(refused.value.message)


async def test_the_monthly_allowance_turns_over_at_the_students_own_midnight(client):
    """
    On the 1st where the student is, not where the server is.

    In Nairobi the last hours of a month are already the next month in UTC. A
    counter filed on the server's clock would refill three hours early at the
    end of every month and, worse, tell a student at 2am on the 1st that they
    are still out of questions.
    """
    from zoneinfo import ZoneInfo

    from app.services.quota import month_key, resets_on
    from app.services.zones import UTC_ZONE

    nairobi = ZoneInfo("Africa/Nairobi")

    last_night = datetime(2026, 3, 31, 23, 30, tzinfo=nairobi)
    just_after = datetime(2026, 4, 1, 0, 30, tzinfo=nairobi)

    assert month_key(last_night, nairobi) == "2026-03"
    assert month_key(just_after, nairobi) == "2026-04"
    # Half past midnight in Nairobi is half past nine the previous evening in
    # UTC. The two clocks disagree about which month it is, and the student's
    # is the one that counts.
    assert month_key(just_after, UTC_ZONE) == "2026-03"

    # And the app is told when to expect the refill.
    assert resets_on("ai_queries", nairobi, last_night) == date(2026, 4, 1)
    assert resets_on("ai_queries_lifetime", nairobi, last_night) is None


async def test_a_students_saved_timezone_is_the_one_periods_are_cut_on(client):
    """
    Someone who travels takes their day boundary with them.

    The zone comes from the settings row, so the reset a student sees is the
    one their own clock predicts rather than the server's.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        # Nobody has opened Settings yet: the column default stands in.
        assert (await user_zone(session, user_id)).key == "Africa/Nairobi"

        session.add(UserSettings(user_id=user_id, timezone="America/New_York"))
        await session.commit()

    async with client.sessions() as session:
        assert (await user_zone(session, user_id)).key == "America/New_York"


async def test_an_unknown_timezone_does_not_break_the_tutor(client):
    """A typo the client let through is a preference, not an outage."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        session.add(UserSettings(user_id=user_id, timezone="Mars/Olympus_Mons"))
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        await check_ai_query(session, user_id, entitlement)
        assert await record_usage(session, user_id, "ai_queries") == 1


async def test_the_free_plan_runs_out_for_good(client):
    """
    The ceiling that makes free affordable to run.

    Five a day bounds the rate and not the bill: an account that never converts
    would otherwise cost five questions a day for as long as it exists. A
    hundred is where the free plan ends, and the refusal has to say that rather
    than telling someone to come back tomorrow for a reset that is never coming.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        ceiling = entitlement.limits.lifetime_ai_queries
        assert ceiling == 100

        await record_usage(session, user_id, "ai_queries_lifetime", ceiling)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        with pytest.raises(AppError) as refused:
            await check_ai_query(session, user_id, entitlement)

    assert "free plan" in str(refused.value.message)


async def test_a_paid_plan_has_no_lifetime_ceiling(client):
    """The month is what bounds a paid plan. Anything else would be a trap."""
    _, user_id = await sign_in(client)
    await give_plan(client, user_id, Tier.PRO)

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        await record_usage(session, user_id, "ai_queries_lifetime", 5000)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)
        # No exception: the counter is kept on every tier, and ignored here.
        await check_ai_query(session, user_id, entitlement)


async def test_a_lapsed_plan_falls_to_free_rather_than_to_nothing(client):
    """
    Expiry is evaluated per request, so the drop is immediate -- but it is a
    drop to the same floor everyone else stands on, not to a tier with nothing
    in it. Someone who paid once and stopped is still a user.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = Tier.PRO.value
        subscription.verified = True
        subscription.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    assert entitlement.tier is Tier.FREE
    # What they had is still reportable, so the app can say which plan ended.
    assert entitlement.nominal_tier is Tier.PRO
    assert entitlement.limits.monthly_ai_queries == 30


async def test_a_trial_still_running_is_left_alone(client):
    """
    The trial is no longer granted, but the accounts inside one were promised a
    fortnight. It keeps its own limits until it runs out, then falls to free
    like everything else.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = Tier.TRIAL.value
        subscription.expires_at = utc_now() + timedelta(days=3)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    trial_questions = plan_for(Tier.TRIAL).limits.monthly_ai_queries
    assert entitlement.tier is Tier.TRIAL
    assert entitlement.limits.monthly_ai_queries == trial_questions


async def test_the_old_expired_tier_still_resolves(client):
    """
    Rows written before free existed have "expired" in the tier column. That
    string has to keep meaning something, and the something is free.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = "expired"
        subscription.expires_at = utc_now() - timedelta(days=1)
        await session.commit()

    async with client.sessions() as session:
        entitlement = await get_entitlement(session, user_id)

    assert entitlement.tier is Tier.FREE


async def test_a_free_account_can_still_read_its_own_work(client):
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
    paid. Until Kora confirms it, that is a claim -- and a claim that
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

    assert entitlement.tier is Tier.FREE
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

    assert entitlement.tier is Tier.FREE


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


async def test_a_counter_collision_does_not_poison_the_transaction():
    """
    Two requests creating the same period's counter must not abort the
    surrounding transaction.

    On Postgres a unique violation poisons the whole transaction and every
    later statement fails with InFailedSQLTransactionError naming an innocent
    query. In production that surfaced as the AI refusing, with a traceback
    pointing at a plain SELECT on usage_counters -- the victim, not the cause.

    Builds its own engine rather than going through `client`, because what is
    under test is a session, not an endpoint.
    """
    import uuid as _uuid
    from datetime import date

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.base import Base
    from app.models.billing import UsageCounter
    from app.services import quota

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = _uuid.uuid4()
    period = quota.METRIC_PERIODS["ai_queries"]()

    try:
        async with sessions() as session:
            # Stand in for the request that got there first.
            session.add(
                UsageCounter(
                    user_id=user_id,
                    metric="ai_queries",
                    period_key=period,
                    count=1,
                    period_date=date.today(),
                )
            )
            await session.flush()

            # Force the "no row yet" path even though one exists — exactly what
            # the losing request of a race sees.
            original = quota._counter
            calls = {"n": 0}

            async def _blind_once(inner, uid, metric, key):
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                return await original(inner, uid, metric, key)

            quota._counter = _blind_once
            try:
                total = await quota.record_usage(session, user_id, "ai_queries")
            finally:
                quota._counter = original

            assert total == 2, "the collision should have added to the existing row"

            # The transaction must still be usable. This is the regression.
            rows = (
                await session.scalars(
                    select(UsageCounter).where(UsageCounter.user_id == user_id)
                )
            ).all()
            assert len(rows) == 1
    finally:
        await engine.dispose()
