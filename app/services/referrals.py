"""
The referral programme.

Every rule is in this module, and every rule hangs off one decision: **nothing
is earned until the person who was referred pays.** Not a signup, not an
install, not an invite accepted.

That matters more here than it would elsewhere. Sign-in is a phone number and
an SMS, so a signup already costs us a text message — a per-signup bounty would
be paying people to run up our own SMS bill, and SIM cards are cheap enough
that a farm is a weekend's work. Tie the reward to a payment Kora has confirmed
and the cheapest attack costs KES 150 to earn about KES 30 of tokens. No fraud
rule polices it half as well as the arithmetic does.

**Paid in days, never in money.** A day of Focus costs us tokens and storage;
a shilling costs us a shilling. Extending a subscriber's plan is deferred
revenue, not lost revenue, and it defers it on the students who churn least.

**Both sides get something, and the friend's half is the referrer's gift.**
Seven days on the plan they just bought turns the pitch from "sign up and pay
so I get free days" into "here, have a week on me". One of those is a favour
you ask in a WhatsApp group; the other is a thing you give, and the difference
is most of the share rate. It happens once, at their first purchase. After
that they are an ordinary account: they earn by referring somebody, like
everyone else.

**A free referrer's days are banked, not lost.** They have no plan to add days
to, so the reward waits for their first payment — and "you have earned 28 free
days, they start when you subscribe" is a much better reason to pay than a
paywall is.
"""

from __future__ import annotations

import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.models.account import User
from app.models.billing import Subscription
from app.models.referral import ReferralReward
from app.services.plans import SEASON_DAYS, Tier, plan_for
from app.services.quota import effective_tier

log = structlog.get_logger()

#: What a referrer earns when their friend buys a 30-day plan.
REWARD_DAYS = 14

#: And when they buy a Season. Four months of revenue arriving at once should
#: pay better than one month of it; the same grant with a bigger number.
SEASON_REWARD_DAYS = 30

#: What the friend gets on the plan they just bought. Their half of the
#: exchange, once, and never again.
FRIEND_DAYS = 7

#: How long a reward sits before it can be credited. A payment that reverses
#: inside this window costs nothing, because nothing has been given yet — and
#: clawing back days somebody has already spent is a support ticket we lose.
VEST_DAYS = 7

#: How long a banked reward keeps its value. Without an end date, a promise
#: made to an account that never converts is a liability carried forever.
BANK_DAYS_TO_LIVE = 90

#: The most a free referrer can hold at once. Generous — five referrals — and
#: still a number rather than an open cheque.
BANK_CAP_DAYS = 60

#: Rewards one account can earn in a rolling month. Nobody referring their
#: actual friends meets this; anything that does is worth looking at by hand.
MAX_REWARDS_PER_MONTH = 3
_MONTH = timedelta(days=30)

#: No I, O, 0 or 1. A code is read off one screen and typed into another, and
#: those four are the only characters that reliably get read as each other.
_ALPHABET = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "IO01"
)
_CODE_LENGTH = 6


@dataclass(frozen=True)
class ReferralSummary:
    """What the profile screen shows."""

    code: str
    #: Friends who signed up with the code. Not all of them have paid.
    joined: int
    #: …and how many of those paid, which is what actually earns anything.
    paid: int
    days_earned: int
    days_banked: int
    #: True while the referrer is on Free, so the app can say what the banked
    #: days are waiting for rather than leaving a number unexplained.
    banked_pending_subscription: bool


# --- The code ----------------------------------------------------------------


async def code_for(session: AsyncSession, user: User) -> str:
    """
    This student's code, minted on first use.

    Lazy rather than at sign-up, because most accounts never open the referral
    screen and a unique column filled in for all of them is a backfill that can
    fail on a collision, bought for nothing.
    """
    if user.referral_code:
        return user.referral_code

    # Six characters from a 32-symbol alphabet is a billion codes. A collision
    # is a retry, not an error — and the unique index is what actually decides,
    # because two requests can pick the same code before either has committed.
    for _ in range(5):
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        try:
            async with session.begin_nested():
                user.referral_code = candidate
                await session.flush()
            return candidate
        except IntegrityError:
            continue

    raise RuntimeError("Could not mint a referral code.")


async def claim(session: AsyncSession, *, user: User, code: str | None) -> bool:
    """
    Records who brought this student in. Once, at first sign-in.

    Refused afterwards, and that is the whole point: a code that can be added
    later is a code somebody adds after they have already paid, which is the
    most-used hole in every referral programme ever run.

    Returns whether the claim stuck. A bad code is not an error the student
    should see — they are in the middle of signing in, and a sign-in that fails
    because a friend mistyped a code is a lost account, not a protected one.
    """
    if not code or user.referred_by_user_id is not None:
        return False

    referrer = await session.scalar(
        select(User).where(
            User.referral_code == code.strip().upper(),
            User.deleted_at.is_(None),
        )
    )

    if referrer is None:
        log.info("referral_code_unknown", code=code)
        return False

    if referrer.id == user.id:
        return False

    user.referred_by_user_id = referrer.id
    await session.flush()
    log.info(
        "referral_claimed", user_id=str(user.id), referrer_id=str(referrer.id)
    )
    return True


# --- Earning -----------------------------------------------------------------


async def on_first_payment(
    session: AsyncSession, *, user_id: uuid.UUID, tier: Tier
) -> None:
    """
    The single hook. Called wherever a payment becomes real.

    Two jobs, because both are triggered by the same event and splitting them
    means two hooks to remember:

    1. If this student was referred, their referrer earns.
    2. If this student has banked rewards of their own, they are released —
       they have just become someone with a plan to add days to.

    Never raises. A referral is a bonus on top of a payment that has already
    succeeded, and there is no version of this failing that should turn a
    successful purchase into an error the student sees.
    """
    try:
        await _award(session, user_id=user_id, tier=tier)
        await release_bank(session, user_id=user_id)
    except Exception:  # noqa: BLE001 — a paid plan must not fail over a bonus
        log.exception("referral_hook_failed", user_id=str(user_id))


async def _award(
    session: AsyncSession, *, user_id: uuid.UUID, tier: Tier
) -> ReferralReward | None:
    payer = await session.get(User, user_id)
    if payer is None or payer.referred_by_user_id is None:
        return None

    # One reward per person referred, ever. The unique constraint is the real
    # guard; this is the cheap read that keeps a second purchase from taking
    # the savepoint path every time.
    existing = await session.scalar(
        select(ReferralReward).where(ReferralReward.referred_user_id == user_id)
    )
    if existing is not None:
        return None

    referrer = await session.get(User, payer.referred_by_user_id)
    if referrer is None or referrer.deleted_at is not None:
        return None

    plan = plan_for(tier)
    days = SEASON_REWARD_DAYS if plan.duration_days >= SEASON_DAYS else REWARD_DAYS
    now = utc_now()

    refused = await _refusal(session, referrer=referrer, payer=payer)

    reward = ReferralReward(
        referrer_id=referrer.id,
        referred_user_id=user_id,
        tier=plan.id.value,
        status="voided" if refused else "pending",
        reason=refused or "",
        days=0 if refused else days,
        friend_days=0 if refused else FRIEND_DAYS,
        vest_at=None if refused else now + timedelta(days=VEST_DAYS),
    )
    session.add(reward)

    if not refused:
        # The friend's half is credited now, not on the hold. It is part of the
        # purchase they just made, and a welcome gift that shows up a week late
        # is not a welcome gift.
        await extend_plan(session, user_id=user_id, days=FRIEND_DAYS)

    await session.flush()
    log.info(
        "referral_earned",
        referrer_id=str(referrer.id),
        referred_id=str(user_id),
        days=reward.days,
        status=reward.status,
        reason=reward.reason,
    )
    return reward


async def _refusal(
    session: AsyncSession, *, referrer: User, payer: User
) -> str | None:
    """
    Why this one does not pay, or ``None`` if it does.

    Refusals are recorded rather than dropped: "my friend paid and I got
    nothing" has to be answerable from the ledger, and a row that was never
    written cannot answer it.
    """
    if referrer.id == payer.id:
        return "self_referral"

    # The careless half of self-referral: one phone, two accounts. A reinstall
    # mints a new device id so this catches nobody determined — but the
    # determined case has to pay us first, which is the actual defence.
    if (
        referrer.active_device_id is not None
        and referrer.active_device_id == payer.active_device_id
    ):
        return "same_device"

    recent = (
        await session.scalar(
            select(func.count())
            .select_from(ReferralReward)
            .where(
                ReferralReward.referrer_id == referrer.id,
                ReferralReward.status != "voided",
                ReferralReward.created_at > utc_now() - _MONTH,
            )
        )
    ) or 0

    if recent >= MAX_REWARDS_PER_MONTH:
        return "monthly_cap"

    return None


# --- Vesting, banking, crediting ---------------------------------------------


async def sweep(session: AsyncSession) -> int:
    """
    Moves rewards through the states time is responsible for.

    Runs on the worker beside the reminder sweep. Two transitions:

    * ``pending`` past its hold becomes ``credited`` if the referrer has a plan
      to add days to, and ``banked`` if they do not.
    * ``banked`` past its expiry becomes ``voided``. An earned reward that was
      never taken up stops being a liability at some point, and ninety days is
      that point.

    Returns how many rows changed, for the log line.
    """
    now = utc_now()
    changed = 0

    due = (
        await session.scalars(
            select(ReferralReward)
            .where(
                ReferralReward.status == "pending",
                ReferralReward.vest_at.is_not(None),
                ReferralReward.vest_at <= now,
            )
            .limit(200)
        )
    ).all()

    for reward in due:
        if await _has_a_plan(session, reward.referrer_id):
            await _credit(session, reward)
        else:
            await _bank(session, reward)
        changed += 1

    stale = (
        await session.scalars(
            select(ReferralReward).where(
                ReferralReward.status == "banked",
                ReferralReward.banked_until.is_not(None),
                ReferralReward.banked_until <= now,
            )
        )
    ).all()

    for reward in stale:
        reward.status = "voided"
        reward.reason = "bank_expired"
        changed += 1

    if changed:
        await session.flush()
        log.info("referral_sweep", changed=changed)

    return changed


async def release_bank(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """
    Turns banked rewards into days, now that there is a plan to put them on.

    Called from the payment hook. Everything still live is credited at once —
    somebody who earned four weeks while on Free and then subscribes should see
    all four, which is what was promised on the screen that persuaded them.
    """
    now = utc_now()
    banked = (
        await session.scalars(
            select(ReferralReward).where(
                ReferralReward.referrer_id == user_id,
                ReferralReward.status == "banked",
            )
        )
    ).all()

    days = 0
    for reward in banked:
        expiry = as_utc(reward.banked_until)
        if expiry is not None and expiry <= now:
            reward.status = "voided"
            reward.reason = "bank_expired"
            continue

        await _credit(session, reward)
        days += reward.days

    if days:
        log.info("referral_bank_released", user_id=str(user_id), days=days)

    return days


async def _credit(session: AsyncSession, reward: ReferralReward) -> None:
    await extend_plan(session, user_id=reward.referrer_id, days=reward.days)
    reward.status = "credited"
    reward.credited_at = utc_now()
    await session.flush()


async def _bank(session: AsyncSession, reward: ReferralReward) -> None:
    """
    Holds a reward for a referrer who is still on Free.

    The cap is applied here rather than at spend time, so the number on the
    screen is the number they will actually get. A reward trimmed to nothing is
    voided with a reason instead of sitting at zero days looking like a bug.
    """
    held = (
        await session.scalar(
            select(func.coalesce(func.sum(ReferralReward.days), 0)).where(
                ReferralReward.referrer_id == reward.referrer_id,
                ReferralReward.status == "banked",
            )
        )
    ) or 0

    room = max(0, BANK_CAP_DAYS - held)
    if room == 0:
        reward.status = "voided"
        reward.reason = "bank_full"
        return

    reward.days = min(reward.days, room)
    reward.status = "banked"
    reward.banked_until = utc_now() + timedelta(days=BANK_DAYS_TO_LIVE)


async def _has_a_plan(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """
    Whether there is a subscription worth adding days to.

    Free is not one. Adding days to the floor does nothing a student can see,
    and the reward would be spent for no effect — which is exactly what banking
    exists to prevent.
    """
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    return effective_tier(subscription) is not Tier.FREE


async def extend_plan(
    session: AsyncSession, *, user_id: uuid.UUID, days: int
) -> None:
    """
    Adds days to the end of a live plan.

    Deliberately additive and deliberately dumb: it never changes a tier, never
    verifies anything, and does nothing at all to an account with no live plan.
    A reward is time on the plan they chose, not a plan we chose for them.
    """
    if days <= 0:
        return

    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    if subscription is None:
        return

    current_end = as_utc(subscription.expires_at)
    if current_end is None or current_end <= utc_now():
        return

    subscription.expires_at = current_end + timedelta(days=days)
    await session.flush()


# --- Reading -----------------------------------------------------------------


async def summary(session: AsyncSession, *, user: User) -> ReferralSummary:
    """Everything the profile screen needs, in three counts and a code."""
    code = await code_for(session, user)

    joined = (
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.referred_by_user_id == user.id, User.deleted_at.is_(None))
        )
    ) or 0

    rows = (
        await session.execute(
            select(ReferralReward.status, func.sum(ReferralReward.days))
            .where(ReferralReward.referrer_id == user.id)
            .group_by(ReferralReward.status)
        )
    ).all()

    by_status = {status: int(total or 0) for status, total in rows}
    paid = (
        await session.scalar(
            select(func.count())
            .select_from(ReferralReward)
            .where(
                ReferralReward.referrer_id == user.id,
                ReferralReward.status != "voided",
            )
        )
    ) or 0

    banked = by_status.get("banked", 0)

    return ReferralSummary(
        code=code,
        joined=joined,
        paid=paid,
        days_earned=by_status.get("credited", 0),
        # Pending days are shown as banked rather than as a fourth number. The
        # student does not care about our seven-day hold; they care what is
        # coming, and it is coming either way.
        days_banked=banked + by_status.get("pending", 0),
        banked_pending_subscription=bool(banked)
        and not await _has_a_plan(session, user.id),
    )
