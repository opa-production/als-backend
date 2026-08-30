from __future__ import annotations

import secrets
import string
import uuid

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.config import settings
from app.core.errors import AppError, NotFound
from app.models.account import User
from app.models.billing import (
    Payment,
    PlanGroup,
    PlanGroupMember,
    Subscription,
)
from app.services.kora import Charge
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


def receipt_email(user: User) -> str:
    """
    An address to open a Kora transaction against.

    Kora requires one on every charge; phone sign-in never collects one.
    Rather than refuse to sell a plan to a student who signed in with a number,
    a stand-in derived from their account id is used. It is stable, so repeat
    payments group under one Kora customer, and it is never written to —
    ``metadata.user_id`` is what actually identifies the payer.
    """
    if user.email:
        return user.email
    return f"student-{user.id.hex[:12]}@{settings.receipt_email_domain}"


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


async def record_pending_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    reference: str,
    tier: Tier,
    amount_kes: int,
) -> Payment:
    """
    Notes that a charge was started, before anyone knows whether it succeeded.

    The row exists so an unconfirmed payment is *visible*. Without it the
    console cannot tell "nobody tried to pay" from "somebody paid and the
    webhook never arrived", and the admin reconcile endpoint has nothing to act
    on, because it looks payments up by reference.

    Idempotent: references are minted per checkout, but a retried request must
    not raise on the unique index.
    """
    existing = await session.scalar(
        select(Payment).where(Payment.reference == reference)
    )
    if existing is not None:
        return existing

    payment = Payment(
        user_id=user_id,
        reference=reference,
        tier=tier.value,
        amount_kes=amount_kes,
        status="pending",
        channel="",
        paid_at=None,
    )
    session.add(payment)
    await session.flush()
    return payment


async def record_payment(
    session: AsyncSession, *, user_id: uuid.UUID, charge: Charge, tier: Tier
) -> tuple[Payment, bool]:
    """
    Stores a charge, once.

    Kora delivers a webhook more than once — that is documented behaviour,
    not a bug — so the reference is unique and a repeat delivery returns the
    existing row. Without that, a plan gets extended by thirty days per retry.
    """
    existing = await session.scalar(
        select(Payment).where(Payment.reference == charge.reference)
    )

    if existing is not None:
        # Already credited: a repeat webhook, or a reconcile after the fact.
        if existing.status == "success":
            return existing, False

        # The pending row written when checkout started, now answered. Kora is
        # the authority on what happened, so its verdict is copied over --
        # including a failure, which is worth recording rather than leaving the
        # row to sit as "pending" forever.
        existing.status = charge.status
        existing.amount_kes = charge.amount_kes
        existing.channel = charge.channel or existing.channel

        if charge.status != "success":
            await session.flush()
            return existing, False

        existing.paid_at = utc_now()
        await session.flush()
        # True, because the plan has not been credited yet: this is the call
        # that must activate it.
        return existing, True

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
    Rounding up would hand out six Friends seats for five seats' money.
    """
    named = (charge.metadata or {}).get("tier")

    if named:
        try:
            claimed = Tier(named)
        except ValueError:
            claimed = None

        if claimed in SELLABLE and charge.amount_kes >= plan_for(claimed).price_ksh:
            return claimed

    # Every sellable plan, dearest first, so the first match is the most
    # expensive thing the money covers. Derived from SELLABLE rather than
    # listed: a hardcoded tuple silently stops resolving the day a plan is
    # added, and the symptom is a paid student left on Free.
    for tier in sorted(SELLABLE, key=lambda t: plan_for(t).price_ksh, reverse=True):
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
    Email is the fallback for a payment made from a Kora page, where the
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


# --- Friends: one payment, six seats -----------------------------------------


async def open_group(
    session: AsyncSession, *, owner_id: uuid.UUID, tier: Tier = Tier.FRIENDS
) -> PlanGroup:
    """
    Opens the owner's group, or brings an existing one up to what they just
    bought.

    The renewal half is the part that matters, and it was missing. A group is
    created once and then found on every later payment, so returning the
    existing row untouched meant a Friends plan renewed for a second month
    extended *the owner's* subscription and nobody else's: the group still
    expired on the old date, every seat with it, and the next friend to tap the
    invite link was told the plan had expired. Six people paid; one of them
    kept working.

    So an existing group is re-dated here, along with the seats sitting in it.
    Only subscriptions that came *from* this group are touched — someone who
    joined while holding their own paid plan keeps it, exactly as they do when
    a seat is taken away.

    ``tier`` is passed in rather than assumed, because Friends now comes in two
    lengths and a group has to expire when the plan the owner actually bought
    does — a Season's group living thirty days would end it three months early
    for everyone on it.
    """
    plan = plan_for(tier)
    group = await session.scalar(
        select(PlanGroup).where(PlanGroup.owner_id == owner_id)
    )

    if group is None:
        # The owner holds one of the six. Anything else would sell six seats
        # and give away seven.
        group = PlanGroup(
            owner_id=owner_id,
            tier=plan.id.value,
            seats=plan.seats,
            invite_code=_new_invite_code(),
            expires_at=new_period_end(plan.id),
        )
        session.add(group)
        await session.flush()

        session.add(PlanGroupMember(group_id=group.id, user_id=owner_id))
        await session.flush()

        return group

    # Renewing, or moving between Friends and Friends Season. The owner's own
    # subscription has already been extended by `activate`, and this is the
    # same date: read it back rather than computing a second one, so the plan
    # and the group cannot end on different days.
    owner_subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == owner_id)
    )
    if owner_subscription is not None and owner_subscription.expires_at is not None:
        group.expires_at = owner_subscription.expires_at

    group.tier = plan.id.value
    # Never below the seats already taken: shrinking a plan out from under
    # someone who is sitting in it is not a thing a renewal should do.
    group.seats = max(plan.seats, await seats_taken(session, group.id))

    # And the seats follow the group. Scoped to `group_id`, so a member who
    # kept their own paid plan when they joined is not touched.
    await session.execute(
        update(Subscription)
        .where(Subscription.group_id == group.id)
        .values(tier=group.tier, expires_at=group.expires_at, verified=True)
    )
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
        and subscription.tier not in (Tier.TRIAL.value, Tier.FREE.value)
        and subscription.verified
        and current_end is not None
        and current_end > utc_now()
    )

    if not own_plan_live:
        # The group's own tier, not a hardcoded Friends: a seat on a Season is
        # a Season, and stamping the monthly tier here would report the wrong
        # plan name back to a member for four months.
        subscription.tier = group.tier
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
        # Back to the free floor, never to the trial. A seat handed round a
        # hostel would otherwise mint a fresh fortnight for each person who
        # left it.
        subscription.tier = Tier.FREE.value
        subscription.group_id = None
        subscription.expires_at = utc_now()

    await session.flush()
