import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.api.deps import AdminRole, ClientIp, DbSession, HttpClient
from app.core.clock import now as utc_now
from app.core.config import settings
from app.core.errors import AppError, NotFound
from app.models.account import User
from app.models.billing import Payment
from app.schemas.admin import ActionResult, AdminPaymentRow, Page
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services.kora import verify_transaction

router = APIRouter()


class _Row(AdminPaymentRow):
    """A payment with enough of the payer attached to be readable in a table."""

    full_name: str = ""
    phone: str | None = None


@router.get("", response_model=Page[_Row], summary="All payments")
async def list_payments(
    session: DbSession,
    status: str | None = Query(
        default=None, description="pending | success | failed | abandoned"
    ),
    tier: str | None = None,
    channel: str | None = Query(default=None, description="card | mobile_money | bank"),
    q: str | None = Query(
        default=None, description="Matches the Kora reference, or the payer."
    ),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[_Row]:
    """
    The ledger, newest first.

    ``status=pending`` on anything older than an hour is the queue that matters:
    a charge the student may well have completed while the webhook never
    arrived. ``POST /payments/{reference}/reconcile`` is what closes each one.
    """
    statement = select(Payment, User).join(User, User.id == Payment.user_id)

    if status:
        statement = statement.where(Payment.status == status)
    if tier:
        statement = statement.where(Payment.tier == tier)
    if channel:
        statement = statement.where(Payment.channel == channel)
    if since:
        statement = statement.where(Payment.created_at >= since)
    if until:
        statement = statement.where(Payment.created_at <= until)
    if q:
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(Payment.reference).like(pattern),
                func.lower(User.full_name).like(pattern),
                func.lower(User.phone).like(pattern),
                func.lower(User.email).like(pattern),
            )
        )

    total = (
        await session.scalar(select(func.count()).select_from(statement.subquery()))
    ) or 0

    rows = (
        await session.execute(
            statement.order_by(Payment.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    items = [
        _Row(
            id=payment.id,
            user_id=payment.user_id,
            reference=payment.reference,
            tier=payment.tier,
            amount_kes=payment.amount_kes,
            status=payment.status,
            channel=payment.channel,
            paid_at=payment.paid_at,
            created_at=payment.created_at,
            full_name=user.full_name,
            phone=user.phone,
        )
        for payment, user in rows
    ]

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{payment_id}", summary="One payment")
async def get_payment(payment_id: uuid.UUID, session: DbSession) -> dict:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFound("No payment with that id.")

    user = await session.get(User, payment.user_id)

    return {
        "payment": AdminPaymentRow.model_validate(payment),
        "user": (
            {
                "id": str(user.id),
                "full_name": user.full_name,
                "phone": user.phone,
                "email": user.email,
            }
            if user
            else None
        ),
    }


@router.post(
    "/{reference}/reconcile",
    response_model=ActionResult,
    summary="Re-check a payment against Kora",
)
async def reconcile(
    reference: str,
    session: DbSession,
    client: HttpClient,
    admin: AdminRole,
    ip: ClientIp,
) -> ActionResult:
    """
    Asks Kora what actually happened, and applies the answer.

    This exists because webhooks are delivered over the internet. A student
    pays, Kora fires the webhook, the request is dropped or the container
    is mid-deploy, and the money is real while the subscription is not. Nothing
    in the system notices — the student is charged and locked out, and the
    first signal is a complaint.

    Kora's own record is the authority, so this endpoint re-reads it and
    re-runs the same activation path the webhook would have. It is safe to call
    repeatedly: ``record_payment`` keys on the reference, so a charge already
    credited is recognised rather than credited twice.
    """
    if not settings.payments_configured:
        raise AppError("Kora is not configured in this environment.")

    payment = await session.scalar(
        select(Payment).where(Payment.reference == reference)
    )
    if payment is None:
        raise NotFound("No payment with that reference.")

    charge = await verify_transaction(client, reference)
    was = payment.status

    user = await session.get(User, payment.user_id)
    if user is None:
        raise NotFound("The account behind that payment is gone.")

    # The stored row already names an owner, so the ownership question here is
    # not "whose payment is this" but "does Kora still agree". A charge
    # whose metadata names a different account means the reference was reused
    # or the row is wrong, and either way this is not a button to press.
    claimed = (charge.metadata or {}).get("user_id")
    if claimed and str(claimed) != str(user.id):
        raise AppError(
            f"Kora attributes {reference} to a different account "
            f"({claimed}). Investigate before crediting anyone.",
            status_code=409,
        )

    tier = billing_service.tier_from_charge(charge)

    # ``record_payment`` keys on the reference and returns the row that already
    # exists — which is this one, in the normal case. It is called anyway so
    # that a charge Kora knows about and this database somehow does not
    # still gets a row.
    _, created = await billing_service.record_payment(
        session, user_id=user.id, charge=charge, tier=tier
    )

    # Refreshed from Kora either way: a charge that was pending here and
    # has since succeeded there is the entire reason anyone calls this.
    payment.status = charge.status
    payment.channel = charge.channel or payment.channel
    payment.amount_kes = charge.amount_kes
    if charge.status == "success" and payment.paid_at is None:
        payment.paid_at = utc_now()

    activated = False
    if charge.status == "success":
        # The same path a live payment takes. Reconciling is not a lesser kind
        # of payment: it is the one a webhook failed to deliver, and it has to
        # open the group and pay the referral exactly as the other two do.
        await billing_service.apply_payment(session, user_id=user.id, tier=tier)
        activated = True

    await session.flush()
    await audit_service.record(
        session,
        admin=admin,
        action="payment.reconciled",
        target_type="payment",
        target_id=payment.id,
        summary=(
            f"Reconciled {reference}: {was} -> {charge.status}"
            + (f", activated {tier.value}" if activated else "")
        ),
        meta={
            "reference": reference,
            "was": was,
            "now": charge.status,
            "amount_kes": charge.amount_kes,
            "activated": activated,
            "created_payment_row": created,
            "user_id": str(user.id),
        },
        ip=ip,
    )

    if activated:
        return ActionResult(
            message=f"Confirmed. {user.full_name or 'The student'} is now on "
            f"{tier.value}."
        )
    return ActionResult(
        ok=False,
        message=f"Kora reports this payment as {charge.status}. Nothing granted.",
    )
