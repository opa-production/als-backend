from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings
from app.core.errors import AppError

log = structlog.get_logger()

VERIFY_URL = "https://api.paystack.co/transaction/verify"
INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"

#: Paystack works in the minor unit. One shilling is a hundred of these.
MINOR_UNITS = 100
CURRENCY = "KES"


@dataclass(frozen=True)
class Charge:
    reference: str
    status: str
    amount_kes: int
    channel: str
    email: str
    #: Whatever was attached at checkout — this is how a payment finds its way
    #: back to a user id and a tier.
    metadata: dict


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    Confirms a webhook actually came from Paystack.

    Without this, the webhook is an open endpoint that grants subscriptions to
    anyone who can guess its URL. HMAC over the **raw body** — re-serialising
    the parsed JSON changes whitespace and key order, and the digest no longer
    matches.
    """
    if not settings.paystack_webhook_secret or not signature:
        return False

    expected = hmac.new(
        settings.paystack_webhook_secret.encode(), raw_body, hashlib.sha512
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class Checkout:
    """A payment page that has been opened for one specific student."""

    authorization_url: str
    reference: str
    access_code: str


def new_reference() -> str:
    """
    The reference for a checkout, minted here.

    It matters that this is server-side. The reference is the handle the app
    later hands to ``/billing/verify``, and one the *client* chose could be a
    reference it saw somewhere else. Because this one is generated alongside
    the metadata that names the payer, a reference and an owner are decided in
    the same breath.
    """
    return f"als_{uuid.uuid4().hex}"


async def initialize_transaction(
    client: httpx.AsyncClient,
    *,
    email: str,
    amount_kes: int,
    reference: str,
    metadata: dict,
    callback_url: str | None = None,
) -> Checkout:
    """
    Opens a payment page for one student and one plan.

    This is what a shared ``paystack.shop/pay/...`` link cannot do. That link
    is the same page for everybody, so the charge it produces carries no user
    id — leaving the server to guess who paid from the email typed into the
    form. Initialising here attaches ``metadata.user_id`` at the point where
    the caller's token has already proved who they are, so both the webhook
    and ``/billing/verify`` can credit the payment without guessing.
    """
    if not settings.paystack_secret_key:
        raise AppError("Payments are not configured on this server.")

    payload: dict = {
        "email": email,
        # Minor unit. Sending shillings would charge a hundredth of the price.
        "amount": amount_kes * MINOR_UNITS,
        "currency": CURRENCY,
        "reference": reference,
        "metadata": metadata,
    }
    if callback_url:
        payload["callback_url"] = callback_url

    response = await client.post(
        INITIALIZE_URL,
        json=payload,
        headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
    )

    if response.status_code >= 400:
        log.warning(
            "paystack_initialize_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("We could not start that payment. Try again shortly.")

    body = response.json().get("data") or {}
    url = body.get("authorization_url")

    if not url:
        log.error("paystack_initialize_no_url", reference=reference)
        raise AppError("We could not start that payment. Try again shortly.")

    return Checkout(
        authorization_url=url,
        # Paystack echoes the reference back. Read from the response rather
        # than assumed, so the app verifies whatever was actually opened.
        reference=body.get("reference", reference),
        access_code=body.get("access_code", ""),
    )


async def verify_transaction(client: httpx.AsyncClient, reference: str) -> Charge:
    """
    Asks Paystack what actually happened to a reference.

    Called rather than trusting the client's word. The app currently writes an
    unverified subscription when a student says they paid; this is the call
    that turns that claim into a fact, and it is the only thing that should.
    """
    if not settings.paystack_secret_key:
        raise AppError("Payments are not configured on this server.")

    response = await client.get(
        f"{VERIFY_URL}/{reference}",
        headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
    )

    if response.status_code >= 400:
        log.warning(
            "paystack_verify_failed",
            reference=reference,
            status=response.status_code,
        )
        raise AppError("That payment could not be verified.", status_code=402)

    body = response.json().get("data") or {}

    return Charge(
        reference=body.get("reference", reference),
        status=body.get("status", "failed"),
        # Paystack works in the minor unit — cents, not shillings. Reading it
        # as shillings would credit a plan for a hundredth of its price.
        amount_kes=int(body.get("amount", 0)) // MINOR_UNITS,
        channel=body.get("channel", ""),
        email=(body.get("customer") or {}).get("email", ""),
        metadata=body.get("metadata") or {},
    )
