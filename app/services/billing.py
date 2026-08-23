from __future__ import annotations

import secrets
import string
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.errors import AppError, NotFound
from app.models.billing import (
    Payment,
    PlanGroup,
    PlanGroupMember,
    Subscription,
)
from app.services.paystack import Charge
from app.services.plans import SELLABLE, Tier, plan_for
from app.services.quota import new_period_end

log = structlog.get_logger()

#: No I, O, 0 or 1. An invite code gets read aloud and typed from a screenshot,
#: and those four are where that goes wrong.
CODE_ALPHABET = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "IO01"
)
CODE_LENGTH = 8


def _new_invite_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


async def get_or_create_subscription(
    session: AsyncSession, user_id: uuid.UUID
) -> Subscription:
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user_id)
    )

    if subscription is None:
        now = utc_now()
        subscription = Subscription(
            user_id=user_id,
            tier=Tier.TRIAL.value,
            started_at=now,
            expires_at=new_period_end(Tier.TRIAL, now),
            verified=True,
        )
        session.add(subscription)
        await session.flush()

    return subscription


async def activate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tier: Tier,
    verified: bool,
    group_id: uuid.UUID | None = None,
) -> Subscription:
    """
    Puts an account on a tier.

    Renewing the tier already held **extends** rather than restarts: a student
    who pays a week early should not lose that week. Switching tiers starts a
    fresh period, because the two plans are not the same thing.
    """
    subscription = await get_or_create_subscription(session, user_id)
    now = utc_now()
    current_end = as_utc(subscription.expires_at)

    same_tier = subscription.tier == tier.value
    still_live = current_end is not None and current_end > now

    base = current_end if (same_tier and still_live) else now

    subscription.tier = tier.value
    subscription.started_at = subscription.started_at if same_tier else now
    subscription.expires_at = new_period_end(tier, base)
    subscription.verified = verified
    subscription.group_id = group_id

    await session.flush()
    log.info(
        "subscription_activated",
        user_id=str(user_id),
        tier=tier.value,
        verified=verified,
    )
    return subscription


async def record_payment(
    session: AsyncSession, *, user_id: uuid.UUID, charge: Charge, tier: Tier
) -> tuple[Payment, bool]:
    """
    Stores a charge, once.

    Paystack delivers a webhook more than once — that is documented behaviour,
    not a bug — so the reference is unique and a repeat delivery returns the
    existing row. Without that, a plan gets extended by thirty days per retry.
    """
    existing = await session.scalar(
        select(Payment).where(Payment.reference == charge.reference)
    )
    if existing is not None:
        return existing, False

    payment = Payment(
        user_id=user_id,
        reference=charge.reference,
        tier=tier.value,
        amount_kes=charge.amount_kes,
        status=charge.status,
        channel=charge.channel,
        paid_at=utc_now() if charge.status == "success" else None,
    )
    session.add(payment)
    await session.flush()
    return payment, True


def tier_from_charge(charge: Charge) -> Tier:
    """
    Which plan was actually paid for.

    Metadata names a tier, but it is **never trusted on its own** — it is a
    field on a checkout we do not fully control, and taking it at face value
    means a KES 10 payment tagged ``tier=pro`` buys Synapse. The amount is the
    fact; metadata only narrows it.

    Resolution is downward: a payment between two plans buys the lower one.
    Rounding up would hand out five Friends seats for four seats' money.
    """
    named = (charge.metadata or {}).get("tier")

    if named:
        try:
            claimed = Tier(named)
        except ValueError:
            claimed = None

        if claimed in SELLABLE and charge.amount_kes >= plan_for(claimed).price_ksh:
            return claimed

    for tier in (Tier.FRIENDS, Tier.PRO, Tier.STANDARD):
        if charge.amount_kes >= plan_for(tier).price_ksh:
            return tier

    raise AppError("That payment does not match any plan.", status_code=402)


def assert_charge_belongs_to(charge: Charge, *, user_id: uuid.UUID, email: str | None):
    """
    Refuses a payment that is not the caller's.

    Without this, ``/billing/verify`` is a free plan for anyone who can obtain
    a reference — and references travel: they appear in receipts, in
    screenshots, in support threads. First-to-claim would let a stranger spend
    someone else's money on their own account.

    Metadata is the strong signal because checkout sets it from the session.
    Email is the fallback for a payment made from a Paystack page, where the
    only thing tying the charge to a person is who they paid as.
    """
    claimed_user = (charge.metadata or {}).get("user_id")

    if claimed_user:
        if str(claimed_user) != str(user_id):
            raise AppError("That payment belongs to another account.", status_code=403)
        return

    if email and charge.email and charge.email.strip().lower() == email.strip().lower():
        return

    raise AppError(
        "We cannot match that payment to your account. Contact support with "
        "the reference.",
        status_code=403,
    )


# --- Friends: one payment, five seats ----------------------------------------


async def create_group(
    session: AsyncSession, *, owner_id: uuid.UUID
) -> PlanGroup:
    """
    Creates the group and seats the payer in it.

    The owner holds one of the five. Anything else would sell five seats and
    give away six.
    """
    existing = await session.scalar(
        select(PlanGroup).where(PlanGroup.owner_id == owner_id)
    )
    if existing is not None:
        return existing

    plan = plan_for(Tier.FRIENDS)
    group = PlanGroup(
        owner_id=owner_id,
        tier=Tier.FRIENDS.value,
        seats=plan.seats,
        invite_code=_new_invite_code(),
        expires_at=new_period_end(Tier.FRIENDS),
    )
    session.add(group)
    await session.flush()

    session.add(PlanGroupMember(group_id=group.id, user_id=owner_id))
    await session.flush()

    return group


async def seats_taken(session: AsyncSession, group_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(PlanGroupMember)
            .where(PlanGroupMember.group_id == group_id)
        )
    ) or 0


async def join_group(
    session: AsyncSession, *, user_id: uuid.UUID, code: str
) -> PlanGroup:
    """
    Takes a seat on someone else's plan.

    Three refusals, each for a different reason: an unknown code, a plan that
    has run out, and a full one. A student who is already a member is returned
    the group rather than an error — tapping an invite link twice is not a
    mistake worth a message.
    """
    group = await session.scalar(
        select(PlanGroup).where(PlanGroup.invite_code == code.strip().upper())
    )
    if group is None:
        raise NotFound("That invite code is not valid.")

    expires = as_utc(group.expires_at)
    if expires is not None and expires <= utc_now():
        raise AppError("That plan has expired.", status_code=402)

    already = await session.scalar(
        select(PlanGroupMember).where(
            PlanGroupMember.group_id == group.id,
            PlanGroupMember.user_id == user_id,
        )
    )
    if already is not None:
        return group

    if await seats_taken(session, group.id) >= group.seats:
        raise AppError("That plan is full.", status_code=402)

    session.add(PlanGroupMember(group_id=group.id, user_id=user_id))
    await session.flush()

    # A seat is only worth anything if it comes with the entitlement, and it
    # ends when the group's own period does.
    subscription = await get_or_create_subscription(session, user_id)

    # Never trample a live plan they already paid for. Someone with their own
    # Synapse who joins a friend's group should not have it silently replaced
    # by a seat that expires sooner — and should get it back when they leave.
    current_end = as_utc(subscription.expires_at)
    own_plan_live = (
        subscription.group_id is None
        and subscription.tier not in (Tier.TRIAL.value, Tier.EXPIRED.value)
        and subscription.verified
        and current_end is not None
        and current_end > utc_now()
    )

    if not own_plan_live:
        subscription.tier = Tier.FRIENDS.value
        subscription.expires_at = group.expires_at
        subscription.verified = True
        subscription.group_id = group.id
        await session.flush()

    return group


async def remove_member(
    session: AsyncSession, *, group: PlanGroup, member_user_id: uuid.UUID
) -> None:
    """
    Frees a seat.

    The owner cannot be removed from their own plan — the seat would be
    unreclaimable and the group would outlive the person paying for it.
    """
    if member_user_id == group.owner_id:
        raise AppError("The owner cannot be removed from their own plan.")

    member = await session.scalar(
        select(PlanGroupMember).where(
            PlanGroupMember.group_id == group.id,
            PlanGroupMember.user_id == member_user_id,
        )
    )
    if member is None:
        raise NotFound("That person is not on this plan.")

    await session.delete(member)

    # Their entitlement came from the group, so it goes with the seat. Their
    # coursework does not — that has always been theirs.
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == member_user_id)
    )
    if subscription is not None and subscription.group_id == group.id:
        # Expired, never back to trial. A seat handed round a hostel would
        # otherwise mint a fresh fortnight for each person who left it.
        subscription.tier = Tier.EXPIRED.value
        subscription.group_id = None
        subscription.expires_at = utc_now()

    await session.flush()
