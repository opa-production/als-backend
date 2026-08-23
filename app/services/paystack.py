from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings
from app.core.errors import AppError

log = structlog.get_logger()

VERIFY_URL = "https://api.paystack.co/transaction/verify"


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
        amount_kes=int(body.get("amount", 0)) // 100,
        channel=body.get("channel", ""),
        email=(body.get("customer") or {}).get("email", ""),
        metadata=body.get("metadata") or {},
    )
