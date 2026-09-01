import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Header, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.config import settings
from app.core.errors import AppError, Forbidden, NotFound
from app.models.account import User
from app.models.billing import Payment, PlanGroup, PlanGroupMember
from app.services import billing as billing_service
from app.services import daraja, paystack
from app.services.kora import (
    SUCCESS,
    build_metadata,
    charge_from_data,
    initialize_transaction,
    new_reference,
    verify_signature,
    verify_transaction,
)
from app.services.plans import PLANS, SELLABLE, Tier, plan_for, saving_percent
from app.services.quota import get_entitlement

log = structlog.get_logger()
router = APIRouter()


class PlanOut(BaseModel):
    id: str
    name: str
    #: Which card this plan belongs on: "focus", "synapse", "friends". A plan
    #: and its Season are one card with the toggle flipped, and this is what
    #: pairs them — the app must not pair them by picking apart the id.
    family: str
    #: "monthly" | "season"
    billing_period: str
    price_ksh: int
    #: Derived, so the total and the per-head figure cannot drift apart.
    price_per_seat_ksh: int
    #: What a Season works out at a month, for the line under the price.
    price_per_month_ksh: int
    #: How much cheaper that is than the monthly plan on the same card. Zero on
    #: the monthly plans themselves, which the app reads as "no badge".
    saving_percent: int
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


class CheckoutRequest(BaseModel):
    tier: str = Field(description="Which plan to buy: standard, pro or friends.")


class CheckoutOut(BaseModel):
    #: Open this in a browser. It is single-use and belongs to one student.
    checkout_url: str
    #: The same URL under its old name.
    #:
    #: Kept for one release because a build already on a student's phone reads
    #: this field, and an app that cannot find it shows a dead Upgrade button
    #: with no way to fix it short of the store. Remove once the deployed
    #: clients are past the version that reads `checkout_url`.
    authorization_url: str
    #: Hand this back to /billing/verify when the browser closes.
    reference: str
    tier: str
    plan_name: str
    amount_ksh: int


class VerifyRequest(BaseModel):
    reference: str = Field(max_length=120, description="Kora transaction reference.")


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

    Six entries: three plans, each monthly and as a Season. They arrive
    together in one call so the pricing toggle swaps a number in place instead
    of going back to the network for a screen the student is already looking
    at. A build that predates the toggle ignores `family` and
    `billing_period`, draws the three monthly plans it recognises, and is
    simply an older, correct screen.
    """
    # Driven by SELLABLE rather than by excluding tiers one at a time. The
    # exclusion list was "not trial", which silently started advertising the
    # expired tier the moment one existed.
    return [
        PlanOut(
            id=plan.id.value,
            name=plan.name,
            family=plan.family,
            billing_period=plan.billing_period,
            price_ksh=plan.price_ksh,
            price_per_seat_ksh=plan.price_per_seat_ksh,
            price_per_month_ksh=plan.price_per_month_ksh,
            saving_percent=saving_percent(plan),
            duration_days=plan.duration_days,
            seats=plan.seats,
        )
        for plan in (PLANS[tier] for tier in SELLABLE)
    ]


@router.get("/subscription", response_model=SubscriptionOut, summary="Your plan")
async def read_subscription(user: CurrentUser, session: DbSession) -> SubscriptionOut:
    """
    The entitlement in force right now.

    A lapsed plan reports as `free` rather than as the tier that was bought,
    because that is what the limits actually are; `nominal_tier` still names
    what ended. `verified` is false when a payment has not been confirmed by
    Kora — the app writes that optimistically and this is where it gets
    reconciled.
    """
    entitlement = await get_entitlement(session, user.id)
    plan = plan_for(entitlement.tier)

    # No countdown on free: it does not run out. A lapsed plan still carries
    # the date it ended, and reporting "0 days left" beside the word Free reads
    # as a plan about to be taken away rather than one that is simply on.
    remaining = None
    if entitlement.tier is not Tier.FREE and entitlement.expires_at is not None:
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


def _sellable(name: str) -> Tier:
    """
    The tier named, if it is one a student may actually buy.

    In one place because three endpoints resolve a tier now, and a plan that is
    sellable on one path and not another is how something gets bought that was
    never meant to be for sale.
    """
    try:
        tier = Tier(name)
    except ValueError:
        tier = None

    if tier not in SELLABLE:
        raise AppError("That is not a plan you can buy.")

    return tier


@router.post("/checkout", response_model=CheckoutOut, summary="Start a payment")
async def start_checkout(
    payload: CheckoutRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> CheckoutOut:
    """
    Opens a Kora checkout for this student and this plan.

    A fixed, shareable payment link cannot do this. Such a link is the same page
    for everyone: the charge that comes back names no account, so the only
    thread tying it to a student is the email they happened to type — and most
    sign in with a phone number and never have one. Everything downstream then
    has to guess, and ``assert_charge_belongs_to`` is left refusing honest
    payments.

    Here the price comes from the server's own plan table and the metadata is
    written from the caller's token, so the charge arrives already knowing who
    it belongs to and what it bought. The reference is returned so the app can
    verify the exact transaction it opened rather than asking the student
    whether they paid.
    """
    tier = _sellable(payload.tier)
    plan = PLANS[tier]

    checkout = await initialize_transaction(
        http,
        email=billing_service.receipt_email(user),
        name=user.full_name or "ALS student",
        # From the plan table, never from the request. A price the client
        # sends is a price the client can choose.
        #
        # Sent in shillings, not cents: Kora charges the major unit. The
        # Paystack integration this replaced multiplied by 100 here, and
        # leaving that in would bill KES 35,000 for a KES 350 plan.
        amount_kes=plan.price_ksh,
        reference=new_reference(),
        # Kora caps metadata at five short-named fields and rejects the nested
        # structure Paystack accepted, so the payload is built in one place.
        metadata=build_metadata(
            user_id=str(user.id), tier=tier.value, plan_name=plan.name
        ),
        narration=f"ALS {plan.name} — 30 days",
        redirect_url=settings.kora_callback_url or None,
        notification_url=settings.webhook_url or None,
    )

    # Recorded now, as pending, rather than only when Kora confirms it.
    #
    # Until this existed a charge that was started and never confirmed left no
    # trace at all: nothing in the console, nothing to reconcile, and no way to
    # tell "nobody tried to pay" from "somebody paid and we never heard".
    # The first real payment on this system landed in exactly that gap -- money
    # taken, no row, and the admin reconcile button useless because it needs a
    # row to act on.
    #
    # record_payment fills this row in when the webhook or a verify arrives.
    await billing_service.record_pending_payment(
        session,
        user_id=user.id,
        reference=checkout.reference,
        tier=tier,
        amount_kes=plan.price_ksh,
    )
    await session.commit()

    log.info(
        "checkout_started",
        user_id=str(user.id),
        tier=tier.value,
        reference=checkout.reference,
    )

    return CheckoutOut(
        checkout_url=checkout.checkout_url,
        authorization_url=checkout.checkout_url,
        reference=checkout.reference,
        tier=tier.value,
        plan_name=plan.name,
        amount_ksh=plan.price_ksh,
    )


@router.post("/verify", response_model=SubscriptionOut, summary="Confirm a payment")
async def verify_payment(
    payload: VerifyRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> SubscriptionOut:
    """
    Checks a reference with Kora and applies the plan.

    Called when the student comes back from checkout. It asks Kora what happened
    rather than believing the app — the device cannot see a charge, so its word
    is a claim and this is what turns a claim into an entitlement.

    Safe to call repeatedly: the reference is unique, so a second call returns
    the same subscription instead of extending it again.
    """
    # An M-Pesa reference belongs to Safaricom, and Kora has never heard of it.
    # Asked anyway, the answer is "no such transaction", which this endpoint
    # would report as "that payment has not gone through" — telling a student
    # who has just been debited that they were not. Sent to the endpoint that
    # can actually answer instead.
    known = await session.scalar(
        select(Payment).where(Payment.reference == payload.reference)
    )
    if known is not None and known.provider == "daraja":
        raise AppError(
            "Check that M-Pesa payment at /billing/mpesa/status.",
            status_code=409,
        )

    # Asked of whoever opened it. A Paystack reference means nothing to Kora and
    # the reverse is equally true, and either way the answer is "no such
    # transaction" — which this endpoint would report to a student who has just
    # been charged as "that payment has not gone through".
    #
    # For cards this call is not a backstop but the settlement path itself:
    # Paystack's webhook goes to the dashboard URL, which on this shared account
    # belongs to another product, so nothing is ever pushed to us.
    by_card = known is not None and known.provider == "paystack"
    provider = "paystack" if by_card else "kora"

    if by_card:
        charge = await paystack.verify_transaction(http, payload.reference)
        # On a shared business a reference that verifies is not automatically
        # ours. Both markers are set by this service at checkout and neither is
        # guessable, so a transaction from the other app cannot buy a plan here.
        if not paystack.is_ours(charge):
            log.warning(
                "paystack_verify_not_ours", reference=payload.reference[:40]
            )
            raise AppError("That payment is not one of ours.", status_code=403)
    else:
        charge = await verify_transaction(http, payload.reference)

    if charge.status != SUCCESS:
        # Kora reports `processing` and `pending` for a charge that is still
        # moving — an M-Pesa prompt the student has not answered yet. Saying
        # "not gone through" is honest for all of them, and the webhook credits
        # the plan the moment it lands.
        raise AppError("That payment has not gone through.", status_code=402)

    # A reference travels — receipts, screenshots, support threads. Without
    # this, anyone holding one could spend someone else's money on their own
    # account, and first-to-claim would decide who got the plan.
    billing_service.assert_charge_belongs_to(
        charge, user_id=user.id, email=user.email
    )

    tier = billing_service.tier_from_charge(charge)
    _, is_new = await billing_service.record_payment(
        session, user_id=user.id, charge=charge, tier=tier, provider=provider
    )

    if is_new:
        await billing_service.apply_payment(session, user_id=user.id, tier=tier)

    return await read_subscription(user, session)


@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Kora webhook",
    include_in_schema=False,
)
async def kora_webhook(
    request: Request,
    session: DbSession,
    x_korapay_signature: str | None = Header(default=None),
) -> dict[str, bool]:
    """
    Where a payment becomes true.

    Not in the schema, because it is not for the app.

    Three things make this safe. The signature is checked before anything is
    parsed for meaning — and it covers **only the ``data`` object**, hashed with
    SHA-256, which is Kora's own convention and not the one Paystack used. The
    reference is unique, so Kora's repeat deliveries credit the plan exactly
    once. And it answers 200 to anything correctly signed, because a non-2xx
    makes Kora retry for hours over something already recorded.
    """
    raw = await request.body()

    if not verify_signature(raw, x_korapay_signature):
        log.warning("kora_webhook_bad_signature")
        raise Forbidden("Invalid signature.")

    event = await request.json()
    name = event.get("event")

    # `charge.failed` is delivered too. It is deliberately not recorded here:
    # the only failed charges worth a row are ones this service opened, and
    # those already have a `pending` row from checkout that the admin reconcile
    # endpoint can settle against Kora's own record.
    if name != "charge.success":
        log.info("kora_webhook_ignored", event=name)
        return {"received": True}

    data = event.get("data") or {}
    charge = charge_from_data(data)

    user_id = (charge.metadata or {}).get("user_id")
    if not user_id:
        # Nothing to credit. Logged rather than rejected: retrying will not make
        # a user id appear, and a 4xx here would have Kora redelivering for
        # hours over a payment nobody can attribute.
        log.error("kora_webhook_no_user", reference=charge.reference)
        return {"received": True}

    try:
        owner = uuid.UUID(str(user_id))
    except ValueError:
        log.error("kora_webhook_bad_user", reference=charge.reference, user_id=user_id)
        return {"received": True}

    try:
        tier = billing_service.tier_from_charge(charge)
    except AppError:
        log.error(
            "kora_webhook_unknown_tier",
            reference=charge.reference,
            amount=charge.amount_kes,
        )
        return {"received": True}

    _, is_new = await billing_service.record_payment(
        session, user_id=owner, charge=charge, tier=tier, provider="kora"
    )

    if is_new and charge.status == SUCCESS:
        await billing_service.apply_payment(session, user_id=owner, tier=tier)

    log.info(
        "kora_webhook_applied",
        reference=charge.reference,
        tier=tier.value,
        first_delivery=is_new,
    )
    return {"received": True}


# --- M-Pesa ------------------------------------------------------------------


class MpesaRequest(BaseModel):
    tier: str = Field(description="Which plan to buy.")
    phone: str = Field(
        max_length=20,
        description="The M-Pesa number to charge. 07…, 01…, +254… all accepted.",
    )


class MpesaOut(BaseModel):
    #: `stk` — a prompt is ringing, poll /billing/mpesa/status.
    #: `redirect` — M-Pesa was unavailable, open `checkout_url` instead.
    #:
    #: The app must branch on this. It is the whole fallback contract, and it is
    #: decided by the server rather than by the client noticing an error,
    #: because "is Daraja healthy right now" is not a question a phone can
    #: answer.
    mode: str
    #: `daraja` or `kora`. For support and for the app's own logging; the
    #: student is never shown it.
    provider: str
    #: Hand this to /billing/mpesa/status while the prompt is on screen, or to
    #: /billing/verify after a redirect checkout closes.
    reference: str
    #: What to show the student. On `stk` this is Safaricom's own wording of
    #: "check your phone", which is worth showing verbatim because it is what
    #: the handset is about to say.
    message: str
    tier: str
    plan_name: str
    amount_ksh: int
    #: The number the prompt went to, normalised, so the app can show "sent to
    #: 254712345678" and a student can spot a typo before waiting two minutes.
    #: Empty on a redirect.
    phone: str = ""
    #: Only on `redirect`. Open it in a browser.
    checkout_url: str | None = None


class MpesaStatusOut(BaseModel):
    #: pending | success | failed
    status: str
    #: What to show. On failure this is the reason, in words a student can act
    #: on: "You cancelled the payment", "You do not have enough M-Pesa balance".
    message: str
    #: True while the prompt is still live. The app keeps polling on this rather
    #: than on `status`, so a slow answer is never drawn as a failure.
    pending: bool
    #: Present once the plan is on. Saves a second call to /billing/subscription
    #: at the one moment the student is watching the screen.
    subscription: SubscriptionOut | None = None


@router.post(
    "/mpesa", response_model=MpesaOut, summary="Pay with M-Pesa (STK push)"
)
async def start_mpesa(
    payload: MpesaRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> MpesaOut:
    """
    Rings the student's phone with an M-Pesa PIN prompt.

    The default way to pay, and the cheapest: this is Safaricom's own API, with
    no processor in the middle taking a percentage of a KES 150 plan.

    Nothing about the money comes from the request. The **amount** is read from
    the server's plan table, so a client cannot choose its own price. The
    **payer** is the caller's token, so the row is bound to an account before
    the phone even rings — which is why the M-Pesa path needs no equivalent of
    `assert_charge_belongs_to`: there is no moment at which the owner is in
    doubt. The only thing the request supplies is which phone to ring, and
    ringing the wrong phone costs an attacker money rather than earning them
    anything.
    """
    tier = _sellable(payload.tier)
    plan = PLANS[tier]

    # Validated before anything else, and before the fallback is considered. A
    # number that is not a Safaricom line cannot be paid from on either path, so
    # refusing here is a message the student can act on rather than a Kora page
    # that fails two steps later.
    phone = daraja.normalise_phone(payload.phone)
    reference = new_reference()

    push = None
    if daraja.configured() and settings.daraja_callback_url:
        try:
            push = await daraja.push_stk(
                http,
                phone=phone,
                # From the plan table, never the request.
                amount_kes=plan.price_ksh,
                reference=reference,
                description=f"ALS {plan.name}",
                callback_url=settings.daraja_callback_url,
            )
        except AppError as error:
            # Daraja is down, rate-limiting, or refusing the shortcode. The
            # student is mid-payment and does not care whose fault it is, so
            # this falls through to Kora rather than surfacing a provider
            # outage as a dead end.
            #
            # Deliberately only `AppError` — everything `daraja` raises for a
            # provider problem is one. A bug in this process should still
            # surface as a 500 rather than quietly routing every student to the
            # more expensive provider for weeks.
            log.warning(
                "mpesa_push_failed_falling_back",
                user_id=str(user.id),
                reference=reference,
                error=error.message,
            )

    if push is None:
        return await _mpesa_fallback(
            user=user, session=session, http=http, tier=tier, plan=plan
        )

    # Written before the student can possibly answer the prompt. This row is
    # the entire authorisation story of the callback endpoint that follows: a
    # callback naming a CheckoutRequestID with no row here is discarded.
    await billing_service.record_pending_payment(
        session,
        user_id=user.id,
        reference=reference,
        tier=tier,
        amount_kes=plan.price_ksh,
        provider="daraja",
        checkout_request_id=push.checkout_request_id,
    )
    await session.commit()

    log.info(
        "mpesa_push_sent",
        user_id=str(user.id),
        tier=tier.value,
        reference=reference,
        checkout_request_id=push.checkout_request_id,
    )

    return MpesaOut(
        mode="stk",
        provider="daraja",
        reference=reference,
        message=push.customer_message,
        tier=tier.value,
        plan_name=plan.name,
        amount_ksh=plan.price_ksh,
        phone=phone,
    )


async def _mpesa_fallback(*, user, session, http, tier: Tier, plan) -> MpesaOut:
    """
    Kora, when Safaricom cannot be reached.

    The last resort, and the word is accurate: it is the same M-Pesa payment
    with a processor's percentage on top, so it exists to stop an outage costing
    a sale rather than as a second way to pay.

    A fresh reference is minted here. Reusing the one the failed push was opened
    under would leave two providers' rows sharing a key, and the unique index
    would turn a fallback into a 500 at the worst possible moment.
    """
    reference = new_reference()

    checkout = await initialize_transaction(
        http,
        email=billing_service.receipt_email(user),
        name=user.full_name or "ALS student",
        amount_kes=plan.price_ksh,
        reference=reference,
        metadata=build_metadata(
            user_id=str(user.id), tier=tier.value, plan_name=plan.name
        ),
        narration=f"ALS {plan.name}",
        redirect_url=settings.kora_callback_url or None,
        notification_url=settings.webhook_url or None,
    )

    await billing_service.record_pending_payment(
        session,
        user_id=user.id,
        reference=checkout.reference,
        tier=tier,
        amount_kes=plan.price_ksh,
        provider="kora",
    )
    await session.commit()

    log.info(
        "mpesa_fell_back_to_kora",
        user_id=str(user.id),
        tier=tier.value,
        reference=checkout.reference,
    )

    return MpesaOut(
        mode="redirect",
        provider="kora",
        reference=checkout.reference,
        message="Continue on the payment page to finish with M-Pesa.",
        tier=tier.value,
        plan_name=plan.name,
        amount_ksh=plan.price_ksh,
        checkout_url=checkout.checkout_url,
    )


@router.get(
    "/mpesa/status",
    response_model=MpesaStatusOut,
    summary="Has the M-Pesa payment gone through",
)
async def mpesa_status(
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
    reference: str = Query(max_length=120),
) -> MpesaStatusOut:
    """
    What the app polls while the prompt is on the student's screen.

    Asks Safaricom directly rather than waiting for the callback. Both routes
    end in the same place, and this one is the reliable half: the callback is a
    single unauthenticated POST that a dropped connection loses for ever, while
    this can be asked again.

    Scoped to the caller's own payments. A reference is not a secret — it is
    printed on screens and pasted into support threads — so looking one up must
    never reveal anything about somebody else's plan.
    """
    payment = await session.scalar(
        select(Payment).where(
            Payment.reference == reference, Payment.user_id == user.id
        )
    )
    if payment is None:
        raise NotFound("We have no record of that payment.")

    if payment.status == "success":
        return MpesaStatusOut(
            status="success",
            message="Payment received.",
            pending=False,
            subscription=await read_subscription(user, session),
        )

    if payment.status == "failed":
        return MpesaStatusOut(
            status="failed",
            message="That payment did not go through. You have not been charged.",
            pending=False,
        )

    if not payment.checkout_request_id:
        raise AppError("That payment cannot be checked.", status_code=409)

    result = await daraja.confirm(http, payment.checkout_request_id)

    if result.pending:
        return MpesaStatusOut(
            status="pending", message=result.message, pending=True
        )

    credited = await billing_service.settle_mpesa(
        session, payment=payment, paid=result.paid, reason=result.message
    )
    if credited:
        await billing_service.apply_payment(
            session, user_id=payment.user_id, tier=Tier(payment.tier)
        )
    await session.commit()

    if result.paid:
        return MpesaStatusOut(
            status="success",
            message="Payment received.",
            pending=False,
            subscription=await read_subscription(user, session),
        )

    return MpesaStatusOut(status="failed", message=result.message, pending=False)


@router.post(
    "/mpesa/callback",
    status_code=status.HTTP_200_OK,
    summary="M-Pesa callback",
    include_in_schema=False,
)
async def mpesa_callback(request: Request, session: DbSession, http: HttpClient) -> dict:
    """
    Where Safaricom says a prompt was answered. **Nothing here is believed.**

    Unlike Kora's webhook there is no signature to check, because Safaricom does
    not sign these — no secret, no header, no mutual TLS. The body is plain JSON
    posted to a URL, and anyone who can reach this endpoint can post one. Taking
    it at its word would mean a free subscription for anybody who guesses a
    `CheckoutRequestID`.

    So this endpoint credits nothing. It does two things:

    1. Looks the `CheckoutRequestID` up among rows **this service created**. No
       row, no further work — that is the authorisation check, and it is why the
       id is indexed.
    2. Asks Safaricom what actually happened, through `daraja.confirm`, and acts
       on *that*. A forged callback claiming success is answered by Safaricom
       saying the prompt was cancelled, and nothing is credited.

    Always 200, even for a body that made no sense. A non-2xx has Safaricom
    redelivering for hours, and there is nothing here a retry could fix.
    """
    try:
        body = await request.json()
    except (ValueError, TypeError):
        log.info("mpesa_callback_unreadable")
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    hint = daraja.read_callback(body)
    if hint is None:
        log.info("mpesa_callback_not_a_callback")
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    payment = await billing_service.payment_for_checkout_request(
        session, hint.checkout_request_id
    )
    if payment is None:
        # Somebody probing, or a callback for a deployment that shares this
        # shortcode. Either way there is nothing of ours to credit.
        log.warning(
            "mpesa_callback_unknown_request",
            checkout_request_id=hint.checkout_request_id[:40],
        )
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    if payment.status == "success":
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    # The hint said something happened. Safaricom decides what.
    result = await daraja.confirm(http, hint.checkout_request_id)

    if result.pending:
        # The query has not caught up with the callback yet. Left pending: the
        # app's polling asks again, and a prompt that really was answered will
        # be confirmed within seconds.
        log.info("mpesa_callback_still_pending", reference=payment.reference)
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    credited = await billing_service.settle_mpesa(
        session,
        payment=payment,
        paid=result.paid,
        receipt=hint.receipt,
        reason=result.message,
    )
    if credited:
        await billing_service.apply_payment(
            session, user_id=payment.user_id, tier=Tier(payment.tier)
        )
    await session.commit()

    log.info(
        "mpesa_callback_settled",
        reference=payment.reference,
        paid=result.paid,
        credited=credited,
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# --- Cards --------------------------------------------------------------------
#
# Paystack, on a business shared with another product. Two consequences, both
# handled here rather than assumed away:
#
#   · The dashboard's callback URL belongs to the other app, so ours is sent per
#     transaction and overrides it for that transaction only.
#   · The dashboard's webhook URL also belongs to the other app, and there is no
#     per-transaction override. This service will never receive a Paystack
#     webhook. Card payments settle by *asking* — on return from checkout, and
#     again from the worker's sweep for anyone who never came back.


class CardRequest(BaseModel):
    tier: str = Field(description="Which plan to buy.")


class CardOut(BaseModel):
    #: Open this in a browser. Single-use and belongs to one student.
    checkout_url: str
    #: Hand back to /billing/verify when the browser closes.
    reference: str
    tier: str
    plan_name: str
    amount_ksh: int


@router.post("/card", response_model=CardOut, summary="Pay by card")
async def start_card(
    payload: CardRequest,
    user: CurrentUser,
    session: DbSession,
    http: HttpClient,
) -> CardOut:
    """
    Opens a Paystack card checkout for this student and this plan.

    The price comes from the server's plan table and `metadata.user_id` is
    written from the caller's token, so the transaction knows whose it is before
    a card number is typed. On an account shared with another product that is
    not just convenient — it is what lets `paystack.is_ours` tell an ALS payment
    from somebody else's, and the reason another app's KES 350 transaction can
    never turn on a plan here.
    """
    tier = _sellable(payload.tier)
    plan = PLANS[tier]

    if not paystack.configured():
        raise AppError(
            "Card payments are not available right now. Try M-Pesa.",
            status_code=503,
        )

    reference = new_reference()

    checkout = await paystack.initialize_transaction(
        http,
        email=billing_service.receipt_email(user),
        # Shillings in, minor units on the wire — the adapter multiplies. Sent
        # from the plan table, never from the request.
        amount_kes=plan.price_ksh,
        reference=reference,
        metadata=build_metadata(
            user_id=str(user.id), tier=tier.value, plan_name=plan.name
        ),
        # Per transaction, which is what bypasses the dashboard default without
        # touching the other app's setting.
        callback_url=settings.paystack_callback_url or None,
    )

    await billing_service.record_pending_payment(
        session,
        user_id=user.id,
        reference=checkout.reference,
        tier=tier,
        amount_kes=plan.price_ksh,
        provider="paystack",
    )
    await session.commit()

    log.info(
        "card_checkout_started",
        user_id=str(user.id),
        tier=tier.value,
        reference=checkout.reference,
    )

    return CardOut(
        checkout_url=checkout.checkout_url,
        reference=checkout.reference,
        tier=tier.value,
        plan_name=plan.name,
        amount_ksh=plan.price_ksh,
    )


@router.get(
    "/card/return",
    summary="Where Paystack sends the student back",
    include_in_schema=False,
)
async def card_return(reference: str = Query(default="", max_length=120)) -> dict:
    """
    The landing page after a card payment, and deliberately almost nothing.

    Paystack redirects a *browser* here, with no session and no token — so this
    cannot credit anything, and must not pretend to. Crediting from an
    unauthenticated GET carrying a reference would be a plan for anyone who can
    read a URL out of somebody's browser history.

    The app is watching for this redirect and calls `/billing/verify` with its
    own token, which is where the payment actually becomes a subscription.
    """
    return {
        "reference": reference,
        "message": "Payment received. Return to the app to finish.",
    }


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

    Only for an account already on a plan that has seats to give — a group
    without a payment behind it is six free Synapse seats. The test is the
    plan's own seat count rather than a named tier, so Friends Season works
    here the day it is added and nothing has to remember to list it.
    """
    entitlement = await get_entitlement(session, user.id)
    plan = plan_for(entitlement.tier)
    if plan.seats <= 1:
        raise Forbidden("A Friends plan is needed before you can invite anyone.")

    group = await billing_service.open_group(
        session, owner_id=user.id, tier=entitlement.tier
    )
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
