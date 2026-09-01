"""
Catching payments that were made and never heard about.

Every provider here has a moment where the news can go missing, and each loses
it differently:

* **Paystack** never tells us at all. Its webhook goes to the one URL on the
  dashboard, and this deployment shares that business with another product, so
  the URL is theirs. A card payment becomes a subscription only because someone
  asks — and if the student closes the tab before the redirect fires, nobody
  does.
* **M-Pesa** posts one unauthenticated callback. A dropped connection, a deploy,
  a restart, and it is gone. The app polls, but only while it is open, and a
  student who pays and immediately locks their phone stops polling.
* **Kora** retries, which is the best of the three, and still not a guarantee.

The shape of the failure is the same in all three cases and it is the worst one
this system has: **the money is real and the subscription is not.** Nothing
notices, because nothing is watching for an absence — the first signal is a
student saying they paid, and by then they have been locked out of something
they bought.

So this sweeps the other way round: it starts from rows that say `pending`,
which is a list of payments this service opened and never saw an answer to, and
asks the provider about each one. It is the same question `/billing/verify` and
the reconcile button ask, asked on a timer instead of by a person.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.models.billing import Payment
from app.services import billing as billing_service
from app.services import daraja, paystack
from app.services.plans import Tier

log = structlog.get_logger()

#: How long a payment must have been pending before it is chased.
#:
#: A card checkout that started ninety seconds ago is a student still typing
#: their card number, and an M-Pesa prompt that age is still on the handset.
#: Asking about either is a wasted request at best; closing one out as failed is
#: a real payment thrown away.
MIN_AGE = timedelta(minutes=5)

#: How far back to look.
#:
#: A day, which is far longer than any of these take to settle, and short enough
#: that the sweep stays a small query. Anything older that is still pending was
#: abandoned, and the console's reconcile button is the right tool for the one
#: case in a thousand that was not.
MAX_AGE = timedelta(hours=24)

#: Per pass. Small on purpose: this runs beside extraction on the same worker,
#: and a hundred provider calls in a row would hold that worker away from
#: reading somebody's lecture notes.
BATCH = 20


async def pending_payments(session: AsyncSession, limit: int = BATCH) -> list[Payment]:
    """Payments this service opened and never saw an answer to."""
    now = utc_now()

    return list(
        (
            await session.scalars(
                select(Payment)
                .where(
                    Payment.status == "pending",
                    Payment.created_at <= now - MIN_AGE,
                    Payment.created_at >= now - MAX_AGE,
                )
                .order_by(Payment.created_at)
                .limit(limit)
            )
        ).all()
    )


async def settle(
    session: AsyncSession, payment: Payment, *, client: httpx.AsyncClient
) -> bool:
    """
    Ask this payment's provider what happened, and act on the answer.

    Returns whether the plan was turned on by this call.

    Every path ends in `billing.apply_payment`, the same function a webhook and
    a manual reconcile end in. A settlement path that activated a plan its own
    way would be a fourth place to remember to open a Friends group and pay a
    referral, and the third one already managed to forget.
    """
    if payment.provider == "daraja":
        return await _settle_mpesa(session, payment, client=client)
    if payment.provider == "paystack":
        return await _settle_card(session, payment, client=client)
    # Kora retries its own webhook, and the reconcile button covers the rest.
    # Chasing it here as well would be a third caller of the same endpoint for
    # the provider least likely to need one.
    return False


async def _settle_mpesa(
    session: AsyncSession, payment: Payment, *, client: httpx.AsyncClient
) -> bool:
    if not daraja.configured() or not payment.checkout_request_id:
        return False

    result = await daraja.confirm(client, payment.checkout_request_id)
    if result.pending:
        # Still on the handset, or Safaricom is not answering. Left alone: the
        # next sweep asks again, and `MAX_AGE` eventually stops asking.
        return False

    credited = await billing_service.settle_mpesa(
        session, payment=payment, paid=result.paid, reason=result.message
    )
    if credited:
        await billing_service.apply_payment(
            session, user_id=payment.user_id, tier=Tier(payment.tier)
        )
    return credited


async def _settle_card(
    session: AsyncSession, payment: Payment, *, client: httpx.AsyncClient
) -> bool:
    """
    The path that matters most, because for cards there is no other one.

    With Paystack's webhook pointed at another product, a student who paid and
    then closed the tab has no route to a subscription except this sweep.
    """
    if not paystack.configured():
        return False

    try:
        charge = await paystack.verify_transaction(client, payment.reference)
    except AppError:
        # Unreachable or refusing. Left pending for the next pass rather than
        # marked failed — a processor having a bad minute is not a student who
        # did not pay.
        return False

    if charge.status == "abandoned" or charge.status == "pending":
        return False

    # Belt and braces on a shared account. The reference came off our own row so
    # it is ours by construction, but this is the one check standing between the
    # other app's transactions and our subscriptions, and it costs nothing to
    # keep it on every path rather than on most of them.
    if charge.status == paystack.SUCCESS and not paystack.is_ours(charge):
        log.error(
            "paystack_sweep_foreign_charge", reference=payment.reference[:40]
        )
        return False

    _, is_new = await billing_service.record_payment(
        session,
        user_id=payment.user_id,
        charge=charge,
        tier=Tier(payment.tier),
        provider="paystack",
    )

    if is_new and charge.status == paystack.SUCCESS:
        await billing_service.apply_payment(
            session, user_id=payment.user_id, tier=Tier(payment.tier)
        )
        return True

    return False


async def sweep(session: AsyncSession, *, client: httpx.AsyncClient) -> int:
    """
    One pass. Returns how many subscriptions it turned on.

    Any number above zero is a student who paid and would otherwise have been
    locked out of what they bought, so it is logged rather than counted
    silently — a sweep that starts finding several a day means a delivery path
    has broken somewhere upstream.
    """
    payments = await pending_payments(session)
    if not payments:
        return 0

    activated = 0
    for payment in payments:
        try:
            if await settle(session, payment, client=client):
                activated += 1
        except Exception:  # noqa: BLE001
            # One unhappy payment must not end the pass. The next nineteen may
            # each be somebody locked out of a plan they paid for.
            log.exception("settlement_failed", reference=payment.reference[:40])
            await session.rollback()

    await session.commit()

    if activated:
        log.warning(
            "settlement_recovered_payments",
            count=activated,
            checked=len(payments),
        )

    return activated
