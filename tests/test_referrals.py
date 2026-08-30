"""
The referral programme.

Every test here is a rule somebody will try to break, or a promise made on a
screen that has to be true. The load-bearing one is the first: nothing is
earned until the person who was referred actually pays.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.billing import Subscription
from app.models.referral import ReferralReward
from app.services import billing as billing_service
from app.services import referrals
from app.services.plans import Tier
from tests.conftest import OTHER_PHONE, PHONE, sign_in

THIRD_PHONE = "+254733333333"


async def _code(client, headers) -> str:
    body = (await client.get("/api/v1/me/referrals", headers=headers)).json()
    return body["code"]


async def _join_with(client, code, phone=OTHER_PHONE):
    """Signs a brand-new student in, carrying a friend's code."""
    sent = await client.post("/api/v1/auth/otp", json={"phone": phone})
    tokens = (
        await client.post(
            "/api/v1/auth/otp/verify",
            json={
                "phone": phone,
                "code": sent.json()["debug_code"],
                "device_id": str(uuid.uuid4()),
                "referral_code": code,
            },
        )
    ).json()

    return {"Authorization": f"Bearer {tokens['access_token']}"}, uuid.UUID(
        tokens["user_id"]
    )


async def _pays(client, user_id, tier=Tier.STANDARD):
    """The payment path every real purchase goes through."""
    async with client.sessions() as session:
        await billing_service.apply_payment(session, user_id=user_id, tier=tier)
        await session.commit()


async def _vest(client):
    """Runs the sweep with the hold already behind us."""
    async with client.sessions() as session:
        await session.execute(
            ReferralReward.__table__.update().values(
                vest_at=utc_now() - timedelta(days=1)
            )
        )
        await referrals.sweep(session)
        await session.commit()


# --- Earning ------------------------------------------------------------------


async def test_a_signup_alone_earns_nothing(client):
    """
    The rule the whole programme rests on.

    Sign-in costs us an SMS, so paying for signups would be paying people to
    run up our own bill — and a farm of them is a weekend's work. Nothing is
    earned until money arrives.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    await _join_with(client, code)

    async with client.sessions() as session:
        rewards = (await session.scalars(select(ReferralReward))).all()

    assert rewards == []

    summary = (await client.get("/api/v1/me/referrals", headers=mine)).json()
    assert summary["joined"] == 1, "the signup is visible"
    assert summary["paid"] == 0, "but it has earned nothing"


async def test_a_paid_referrer_gets_days_on_the_plan_they_hold(client):
    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)

    async with client.sessions() as session:
        before = as_utc(
            (
                await session.scalar(
                    select(Subscription).where(
                        Subscription.user_id == my_id
                    )
                )
            ).expires_at
        )

    await _pays(client, friend_id)
    await _vest(client)

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=mine)
    ).json()
    after = as_utc(datetime.fromisoformat(subscription["expires_at"]))

    assert (after - before).days == referrals.REWARD_DAYS


async def test_a_season_referral_pays_more(client):
    """Four months of revenue arriving at once should pay better than one."""
    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id, Tier.PRO_SEASON)

    async with client.sessions() as session:
        reward = await session.scalar(select(ReferralReward))

    assert reward.days == referrals.SEASON_REWARD_DAYS


async def test_the_friend_gets_their_week_at_once(client):
    """
    The referrer's gift, not a second payout.

    It is what turns "pay so I get free days" into "have a week on me", and a
    welcome gift that arrives a week late is not a welcome gift — so unlike the
    referrer's half it does not wait for the hold.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    friend, friend_id = await _join_with(client, code)
    await _pays(client, friend_id, Tier.STANDARD)

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend)
    ).json()

    # Thirty days of Focus plus the seven they were given.
    assert subscription["days_remaining"] >= 30 + referrals.FRIEND_DAYS - 1


async def test_being_referred_earns_nothing_further(client):
    """
    A referred student who paid is an ordinary account afterwards. They earn by
    referring somebody, like everyone else.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    friend, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)
    await _vest(client)

    theirs = (await client.get("/api/v1/me/referrals", headers=friend)).json()
    assert theirs["days_earned"] == 0
    assert theirs["days_banked"] == 0
    assert theirs["code"], "but they can refer, starting now"


# --- Banking ------------------------------------------------------------------


async def test_a_free_referrer_banks_the_days(client):
    """
    Days on a plan they do not have would be spent for no effect. Held instead,
    which is also the best reason to subscribe they will ever be given.
    """
    mine, my_id = await sign_in(client)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)
    await _vest(client)

    summary = (await client.get("/api/v1/me/referrals", headers=mine)).json()
    assert summary["days_earned"] == 0
    assert summary["days_banked"] == referrals.REWARD_DAYS
    assert summary["banked_pending_subscription"] is True


async def test_the_bank_is_released_when_they_subscribe(client):
    """The promise on the screen: they start the day you subscribe."""
    mine, my_id = await sign_in(client)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)
    await _vest(client)

    await _pays(client, my_id, Tier.STANDARD)

    summary = (await client.get("/api/v1/me/referrals", headers=mine)).json()
    assert summary["days_banked"] == 0
    assert summary["days_earned"] == referrals.REWARD_DAYS

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=mine)
    ).json()
    assert subscription["days_remaining"] >= 30 + referrals.REWARD_DAYS - 1


async def test_a_banked_reward_does_not_wait_forever(client):
    """
    An open promise to an account that never converts is a liability with no
    end date.
    """
    mine, my_id = await sign_in(client)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)
    await _vest(client)

    async with client.sessions() as session:
        await session.execute(
            ReferralReward.__table__.update().values(
                banked_until=utc_now() - timedelta(days=1)
            )
        )
        await referrals.sweep(session)
        await session.commit()

    summary = (await client.get("/api/v1/me/referrals", headers=mine)).json()
    assert summary["days_banked"] == 0

    # And subscribing later does not resurrect it.
    await _pays(client, my_id)
    summary = (await client.get("/api/v1/me/referrals", headers=mine)).json()
    assert summary["days_earned"] == 0


# --- The rules ----------------------------------------------------------------


async def test_a_code_cannot_be_added_after_the_fact(client):
    """
    The most-used hole in every referral programme: sign up, pay, then claim a
    friend's code. Attribution is written once, at first sign-in.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    # An existing account signs in again, this time carrying the code.
    friend, friend_id = await sign_in(client, phone=OTHER_PHONE)
    await _join_with(client, code, phone=OTHER_PHONE)

    async with client.sessions() as session:
        friend_row = await session.get(User, friend_id)
        assert friend_row.referred_by_user_id is None

    await _pays(client, friend_id)

    async with client.sessions() as session:
        assert (await session.scalars(select(ReferralReward))).all() == []


async def test_you_cannot_refer_yourself(client):
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    async with client.sessions() as session:
        user = await session.get(User, my_id)
        assert await referrals.claim(session, user=user, code=code) is False


async def test_two_accounts_on_one_handset_do_not_pay(client):
    """
    The careless half of self-referral. It catches nobody determined — but the
    determined version has to pay us first, which is the actual defence.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)

    async with client.sessions() as session:
        me = await session.get(User, my_id)
        friend = await session.get(User, friend_id)
        friend.active_device_id = me.active_device_id
        await session.commit()

    await _pays(client, friend_id)

    async with client.sessions() as session:
        reward = await session.scalar(select(ReferralReward))

    assert reward.status == "voided"
    assert reward.reason == "same_device"
    # Recorded rather than dropped: "my friend paid and I got nothing" has to
    # be answerable from the ledger.
    assert reward.days == 0


async def test_one_friend_pays_once_however_often_they_buy(client):
    """Renewing is not a second introduction."""
    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)
    await _pays(client, friend_id)
    await _pays(client, friend_id)

    async with client.sessions() as session:
        rewards = (await session.scalars(select(ReferralReward))).all()

    assert len(rewards) == 1


async def test_an_unknown_code_never_breaks_a_sign_in(client):
    """
    A student mistyping their friend's code must still get an account. A
    sign-in that fails over a referral is a lost account, not a protected one.
    """
    headers, user_id = await _join_with(client, "NOSUCH")

    assert headers["Authorization"]
    async with client.sessions() as session:
        assert (await session.get(User, user_id)).referred_by_user_id is None


async def test_the_monthly_cap_holds(client):
    """Nobody referring their actual friends meets this."""
    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    phones = [OTHER_PHONE, THIRD_PHONE, "+254744444444", "+254755555555"]
    for phone in phones:
        _, friend_id = await _join_with(client, code, phone=phone)
        await _pays(client, friend_id)

    async with client.sessions() as session:
        rewards = (await session.scalars(select(ReferralReward))).all()

    paid = [r for r in rewards if r.status != "voided"]
    refused = [r for r in rewards if r.status == "voided"]

    assert len(paid) == referrals.MAX_REWARDS_PER_MONTH
    assert [r.reason for r in refused] == ["monthly_cap"]


async def test_a_code_is_stable_once_minted(client):
    """Students share it in a WhatsApp group. It cannot move afterwards."""
    headers, _ = await sign_in(client)

    first = await _code(client, headers)
    second = await _code(client, headers)

    assert first == second
    assert len(first) == 6
    # Nothing that gets misread between one screen and another.
    assert not set(first) & set("IO01")


async def test_the_referral_screen_needs_a_token(client):
    assert (await client.get("/api/v1/me/referrals")).status_code == 401


async def test_a_referral_failure_never_costs_a_student_their_plan(client, monkeypatch):
    """
    A bonus on top of a payment that already succeeded. If this ever throws,
    the plan still activates.
    """
    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)

    async def explode(*args, **kwargs):
        raise RuntimeError("referral ledger is having a day")

    monkeypatch.setattr(referrals, "_award", explode)
    await _pays(client, friend_id, Tier.PRO)

    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.user_id == friend_id
            )
        )

    assert subscription.tier == "pro"
    assert subscription.verified is True


async def test_every_payment_path_goes_through_one_hook(client):
    """
    There are three ways a payment becomes real -- /billing/verify, the Kora
    webhook, and the admin reconcile route -- and anything written at the call
    sites has to be remembered three times. It already went wrong twice: group
    creation missed Friends Season, and the reconcile route silently kept its
    own copy after the referral hook was added.

    So this asserts the shape rather than the behaviour: none of the three may
    call `activate` directly. `apply_payment` is the list.
    """
    import inspect

    from app.api.v1.routes import billing as billing_routes
    from app.api.v1.routes.admin import payments as payment_routes

    for module in (billing_routes, payment_routes):
        source = inspect.getsource(module)
        assert "billing_service.activate(" not in source, module.__name__
        assert "apply_payment(" in source, module.__name__


async def test_phone_and_email_stay_out_of_the_referral_payload(client):
    """The screen shows a code and some counts. Nothing that identifies anyone."""
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/me/referrals", headers=headers)).json()

    assert PHONE not in str(body)
    assert set(body) == {
        "code",
        "joined",
        "paid",
        "days_earned",
        "days_banked",
        "banked_pending_subscription",
        "friend_days",
    }


# --- The console --------------------------------------------------------------


async def test_the_console_reads_the_ledger_with_both_sides_named(client):
    """
    Every support question about this programme is about a pair, so a row that
    knows only one of them cannot answer one.
    """
    from tests.test_admin import admin_headers

    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)

    admin = await admin_headers(client)
    page = (await client.get("/api/v1/admin/referrals", headers=admin)).json()

    assert page["total"] == 1
    row = page["items"][0]
    assert row["referrer_id"] == str(my_id)
    assert row["referred_user_id"] == str(friend_id)
    assert row["status"] == "pending"
    assert row["days"] == referrals.REWARD_DAYS


async def test_the_console_can_find_a_referral_from_either_side(client):
    """
    A support thread starts with one account and is usually about the other.
    Asking which end you are holding is asking you to guess.
    """
    from tests.test_admin import admin_headers

    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    await _pays(client, friend_id)

    admin = await admin_headers(client)
    for user_id in (my_id, friend_id):
        found = (
            await client.get(
                f"/api/v1/admin/referrals?user_id={user_id}", headers=admin
            )
        ).json()
        assert found["total"] == 1, user_id


async def test_the_console_says_why_a_referral_paid_nothing(client):
    """The refusal is the answer, so it has to be readable without SQL."""
    from tests.test_admin import admin_headers

    mine, my_id = await sign_in(client)
    await _pays(client, my_id)
    code = await _code(client, mine)

    _, friend_id = await _join_with(client, code)
    async with client.sessions() as session:
        me = await session.get(User, my_id)
        friend = await session.get(User, friend_id)
        friend.active_device_id = me.active_device_id
        await session.commit()
    await _pays(client, friend_id)

    admin = await admin_headers(client)
    page = (
        await client.get(
            "/api/v1/admin/referrals?reason=same_device", headers=admin
        )
    ).json()

    assert page["total"] == 1
    assert page["items"][0]["status"] == "voided"


async def test_the_console_stats_count_signups_and_payers_apart(client):
    """
    Signups against payers is whether the programme works. Conflating them
    would make it look like it always does.
    """
    from tests.test_admin import admin_headers

    mine, my_id = await sign_in(client)
    await _pays(client, my_id, Tier.PRO)
    code = await _code(client, mine)

    _, paying = await _join_with(client, code)
    await _join_with(client, code, phone=THIRD_PHONE)
    await _pays(client, paying)

    admin = await admin_headers(client)
    stats = (await client.get("/api/v1/admin/referrals/stats", headers=admin)).json()

    assert stats["referred_signups"] == 2
    assert stats["referred_payers"] == 1
    assert stats["rewards_by_status"]["pending"] == 1
    assert stats["top_referrers"][0]["user_id"] == str(my_id)


async def test_a_student_token_cannot_read_the_referral_ledger(client):
    headers, _ = await sign_in(client)
    response = await client.get("/api/v1/admin/referrals", headers=headers)
    assert response.status_code in (401, 403)
