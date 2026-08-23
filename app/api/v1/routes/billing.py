import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.errors import AppError, Forbidden, NotFound
from app.models.account import User
from app.models.billing import PlanGroup, PlanGroupMember
from app.services import billing as billing_service
from app.services.paystack import verify_signature, verify_transaction
from app.services.plans import PLANS, SELLABLE, Tier, plan_for
from app.services.quota import get_entitlement

log = structlog.get_logger()
router = APIRouter()


class PlanOut(BaseModel):
    id: str
    name: str
    price_ksh: int
    #: Derived, so the total and the per-head figure cannot drift apart.
    price_per_seat_ksh: int
    duration_days: int
    seats: int


class SubscriptionOut(BaseModel):
    #: What is in force. "expired" once a trial or a paid period has run out.
    tier: str
    name: str
    #: What it *was*, so the app can say "your Synapse plan ended" rather than
    #: leaving a student to guess what they lost.
    nominal_tier: str
    expires_at: datetime | None
    days_remaining: int | None
    verified: bool
    seats: int
    #: True when nothing metered is available any more.
    is_expired: bool


class VerifyRequest(BaseModel):
    reference: str = Field(max_length=120, description="Paystack transaction reference.")


class GroupOut(BaseModel):
    id: uuid.UUID
    invite_code: str
    seats: int
    seats_taken: int
    expires_at: datetime | None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    is_owner: bool


class JoinRequest(BaseModel):
    code: str = Field(max_length=12)


@router.get("/plans", response_model=list[PlanOut], summary="What is for sale")
async def list_plans() -> list[PlanOut]:
    """
    The plans, served from the same config the limits are enforced from.

    The app ships its own copy so it works offline; this endpoint is what lets
    a price change reach a phone without an app store release.
    """
    # Driven by SELLABLE rather than by excluding tiers one at a time. The
    # exclusion list was "not trial", which silently started advertising the
    # expired tier the moment one existed.
    return [
        PlanOut(
            id=PLANS[tier].id.value,
            name=PLANS[tier].name,
            price_ksh=PLANS[tier].price_ksh,
            price_per_seat_ksh=PLANS[tier].price_per_seat_ksh,
            duration_days=PLANS[tier].duration_days,
            seats=PLANS[tier].seats,
        )
        for tier in SELLABLE
    ]


@router.get("/subscription", response_model=SubscriptionOut, summary="Your plan")
async def read_subscription(user: CurrentUser, session: DbSession) -> SubscriptionOut:
    """
    The entitlement in force right now.

    An expired plan reports as `trial` rather than as the tier that was bought,
    because that is what the limits actually are. `verified` is false when a
    payment has not been confirmed by Paystack — the app writes that optimistically
    and this is where it gets reconciled.
    """
    entitlement = await get_entitlement(session, user.id)
    plan = plan_for(entitlement.tier)

    remaining = None
    if entitlement.expires_at is not None:
        from app.core.clock import now as utc_now

        remaining = max(0, (entitlement.expires_at - utc_now()).days)

    return SubscriptionOut(
        tier=entitlement.tier.value,
        name=plan.name,
        nominal_tier=entitlement.nominal_tier.value,
        expires_at=entitlement.expires_at,
        days_remaining=remaining,
        verified=entitlement.verified,
        seats=plan.seats,
        is_expired=entitlement.is_expired,
    )


@router.post("/verify", response_model=SubscriptionOut, summary="Confirm a payment")
async def verify_payment(
    payload: VerifyRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> SubscriptionOut:
    """
    Checks a reference with Paystack and applies the plan.

    Called when the student comes back from checkout. It asks Paystack what
    happened rather than believing the app — the device cannot see a charge, so
    its word is a claim and this is what turns a claim into an entitlement.

    Safe to call repeatedly: the reference is unique, so a second call returns
    the same subscription instead of extending it again.
    """
    charge = await verify_transaction(http, payload.reference)

    if charge.status != "success":
        raise AppError("That payment has not gone through.", status_code=402)

    # A reference travels — receipts, screenshots, support threads. Without
    # this, anyone holding one could spend someone else's money on their own
    # account, and first-to-claim would decide who got the plan.
    billing_service.assert_charge_belongs_to(
        charge, user_id=user.id, email=user.email
    )

    tier = billing_service.tier_from_charge(charge)
    _, is_new = await billing_service.record_payment(
        session, user_id=user.id, charge=charge, tier=tier
    )

    if is_new:
        await billing_service.activate(
            session, user_id=user.id, tier=tier, verified=True
        )
        if tier is Tier.FRIENDS:
            await billing_service.create_group(session, owner_id=user.id)

    return await read_subscription(user, session)


@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Paystack webhook",
    include_in_schema=False,
)
async def paystack_webhook(
    request: Request,
    session: DbSession,
    x_paystack_signature: str | None = Header(default=None),
) -> dict[str, bool]:
    """
    Where a payment becomes true.

    Not in the schema, because it is not for the app.

    Three things make this safe. The signature is checked against the **raw
    body** — re-serialising parsed JSON changes whitespace and breaks the
    digest. The reference is unique, so Paystack's repeat deliveries credit the
    plan once. And it always answers 200 on a signed request, because a
    non-2xx makes Paystack retry for hours over something we have already
    recorded.
    """
    raw = await request.body()

    if not verify_signature(raw, x_paystack_signature):
        log.warning("paystack_webhook_bad_signature")
        raise Forbidden("Invalid signature.")

    event = await request.json()
    if event.get("event") != "charge.success":
        return {"received": True}

    data = event.get("data") or {}
    metadata = data.get("metadata") or {}

    user_id = metadata.get("user_id")
    if not user_id:
        # Nothing to credit. Logged rather than rejected: retrying will not
        # make a user id appear.
        log.error("paystack_webhook_no_user", reference=data.get("reference"))
        return {"received": True}

    from app.services.paystack import Charge

    charge = Charge(
        reference=data.get("reference", ""),
        status=data.get("status", "failed"),
        amount_kes=int(data.get("amount", 0)) // 100,
        channel=data.get("channel", ""),
        email=(data.get("customer") or {}).get("email", ""),
        metadata=metadata,
    )

    try:
        tier = billing_service.tier_from_charge(charge)
    except AppError:
        log.error("paystack_webhook_unknown_tier", reference=charge.reference)
        return {"received": True}

    _, is_new = await billing_service.record_payment(
        session, user_id=uuid.UUID(user_id), charge=charge, tier=tier
    )

    if is_new and charge.status == "success":
        await billing_service.activate(
            session, user_id=uuid.UUID(user_id), tier=tier, verified=True
        )
        if tier is Tier.FRIENDS:
            await billing_service.create_group(session, owner_id=uuid.UUID(user_id))

    return {"received": True}


# --- Friends -----------------------------------------------------------------


async def _owned_group(session: DbSession, user: User) -> PlanGroup:
    group = await session.scalar(
        select(PlanGroup).where(PlanGroup.owner_id == user.id)
    )
    if group is None:
        raise NotFound("You do not have a Friends plan.")
    return group


@router.post("/group", response_model=GroupOut, summary="Create your Friends plan")
async def create_group(user: CurrentUser, session: DbSession) -> GroupOut:
    """
    Opens the group and seats you in it.

    Only for an account already on Friends — a group without a payment behind
    it is five free Synapse seats.
    """
    entitlement = await get_entitlement(session, user.id)
    if entitlement.tier is not Tier.FRIENDS:
        raise Forbidden("A Friends plan is needed before you can invite anyone.")

    group = await billing_service.create_group(session, owner_id=user.id)
    return GroupOut(
        id=group.id,
        invite_code=group.invite_code,
        seats=group.seats,
        seats_taken=await billing_service.seats_taken(session, group.id),
        expires_at=group.expires_at,
    )


@router.get("/group", response_model=GroupOut, summary="Your invite code")
async def read_group(user: CurrentUser, session: DbSession) -> GroupOut:
    group = await _owned_group(session, user)
    return GroupOut(
        id=group.id,
        invite_code=group.invite_code,
        seats=group.seats,
        seats_taken=await billing_service.seats_taken(session, group.id),
        expires_at=group.expires_at,
    )


@router.post("/group/join", response_model=GroupOut, summary="Join with a code")
async def join_group(
    payload: JoinRequest, user: CurrentUser, session: DbSession
) -> GroupOut:
    """
    Takes a seat on a friend's plan.

    Tapping the same invite twice is not an error — the group comes back
    unchanged rather than a message nobody needed.
    """
    group = await billing_service.join_group(session, user_id=user.id, code=payload.code)
    return GroupOut(
        id=group.id,
        invite_code=group.invite_code,
        seats=group.seats,
        seats_taken=await billing_service.seats_taken(session, group.id),
        expires_at=group.expires_at,
    )


@router.get("/group/members", response_model=list[MemberOut], summary="Who is on it")
async def list_members(user: CurrentUser, session: DbSession) -> list[MemberOut]:
    group = await _owned_group(session, user)

    rows = (
        await session.execute(
            select(PlanGroupMember.user_id, User.full_name)
            .join(User, User.id == PlanGroupMember.user_id)
            .where(PlanGroupMember.group_id == group.id)
        )
    ).all()

    return [
        MemberOut(
            user_id=member_id,
            full_name=name or "",
            is_owner=member_id == group.owner_id,
        )
        for member_id, name in rows
    ]


@router.delete(
    "/group/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone",
)
async def remove_member(
    member_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    """
    Frees a seat.

    Their coursework stays theirs — only the entitlement came from the group,
    and only the entitlement goes back.
    """
    group = await _owned_group(session, user)
    await billing_service.remove_member(
        session, group=group, member_user_id=member_id
    )
