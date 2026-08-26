"""
Kora (korahq.com) — the payment provider.

Replaces Paystack. The shape of the integration is the same — open a checkout
for one student, verify a reference, receive a webhook — but three details
differ in ways that are silent and expensive if missed, so each is called out
where it happens:

1. **Amount is in the major unit.** Kora charges what you send: ``350`` is
   KES 350. Paystack took the minor unit, so the old code multiplied by 100.
   Carrying that habit over here charges every student a hundred times the
   price of their plan.

2. **The webhook signature covers only the ``data`` object**, not the whole
   body, and it is SHA-256 rather than SHA-512. Signing the raw body — the
   correct thing to do for Paystack — produces a digest that never matches, and
   the symptom is a webhook endpoint that rejects every genuine delivery.

3. **The checkout link is ``checkout_url``**, not ``authorization_url``.

Everything here is per Kora's published contract:
https://developers.korapay.com/docs/checkout-redirect
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings
from app.core.errors import AppError

log = structlog.get_logger()

API_ROOT = "https://api.korapay.com/merchant/api/v1"
INITIALIZE_URL = f"{API_ROOT}/charges/initialize"
VERIFY_URL = f"{API_ROOT}/charges"

CURRENCY = "KES"

#: What a Kenyan student will actually reach for, in the order Kora should
#: offer them. Mobile money is M-Pesa here and is the reason this list is not
#: left to the dashboard default — card first would put the least-used option
#: in front of almost everybody.
CHANNELS = ["mobile_money", "card", "bank_transfer"]
DEFAULT_CHANNEL = "mobile_money"

#: Kora treats anything other than these as not-yet-money.
SUCCESS = "success"


@dataclass(frozen=True)
class Charge:
    """
    One transaction, as Kora describes it.

    Deliberately the same shape the Paystack adapter returned, so everything
    downstream — ``tier_from_charge``, ``record_payment``, the admin reconcile
    endpoint — needed no change when the provider did. The provider is an
    implementation detail of this module and nothing above it should know which
    one is in use.
    """

    reference: str
    status: str
    amount_kes: int
    channel: str
    email: str
    #: Whatever was attached at checkout — this is how a payment finds its way
    #: back to a user id and a tier.
    metadata: dict


@dataclass(frozen=True)
class Checkout:
    """A payment page that has been opened for one specific student."""

    checkout_url: str
    reference: str


# --- Amounts -----------------------------------------------------------------


def to_shillings(value: object) -> int:
    """
    Kora's amount, as whole shillings.

    Defensive about the type because Kora returns the amount as a JSON number
    on some responses and a decimal *string* on others (``"350.00"``). An
    ``int()`` straight over that raises, and an unhandled raise inside a webhook
    means the delivery is retried for hours over a payment that already
    succeeded.

    No division by 100. See the module docstring — this is the difference that
    would charge a hundred times the price.
    """
    if value is None:
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        log.warning("kora_unreadable_amount", value=repr(value)[:80])
        return 0


# --- Webhook signatures -------------------------------------------------------

#: Matches the top-level ``"data":`` key so its value can be sliced out of the
#: body byte-for-byte.
_DATA_KEY = re.compile(r'"data"\s*:\s*')


def _data_segment(raw_body: bytes) -> str | None:
    """
    The exact text of the ``data`` value, as it arrived on the wire.

    Kora signs ``JSON.stringify(payload.data)``. The obvious Python translation
    is to parse the body and re-serialise ``data`` compactly — and it *usually*
    works, which is what makes it dangerous. It breaks whenever a round trip
    through Python is not byte-identical to the original: ``350.00`` parses to a
    float and re-serialises as ``350.0``, an integer-valued float becomes
    ``1.0`` where JavaScript wrote ``1``, and any non-ASCII character comes back
    escaped differently.

    Slicing the original bytes sidesteps all of it. ``raw_decode`` is what finds
    the end of the value without needing to balance braces by hand, including
    when the object contains strings with braces in them.
    """
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return None

    decoder = json.JSONDecoder()
    for match in _DATA_KEY.finditer(text):
        start = match.end()
        try:
            _, end = decoder.raw_decode(text, start)
        except ValueError:
            # A `"data":` that appears inside a string literal earlier in the
            # body. Keep looking rather than giving up.
            continue
        return text[start:end]

    return None


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    Confirms a webhook actually came from Kora.

    Without this the webhook is an open endpoint that grants subscriptions to
    anyone who can guess its URL.

    The signed material is **only the ``data`` object**, not the whole payload —
    a Kora-specific choice, and the one thing most likely to be got wrong when
    porting from another provider. The key is the secret API key; Kora does not
    issue a separate webhook secret, so ``KORA_WEBHOOK_SECRET`` exists only for
    the case where someone wants to pin a different value.
    """
    secret = settings.kora_webhook_secret or settings.kora_secret_key
    if not secret or not signature:
        return False

    segment = _data_segment(raw_body)

    if segment is None:
        # The body did not contain a readable `data` value. Re-serialising is
        # the fallback rather than the default, for the reasons in
        # `_data_segment` — and if there is no `data` at all there is nothing
        # here worth acting on anyway.
        try:
            parsed = json.loads(raw_body)
        except ValueError:
            return False
        if "data" not in parsed:
            return False
        segment = json.dumps(parsed["data"], separators=(",", ":"))

    expected = hmac.new(secret.encode(), segment.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


# --- References ---------------------------------------------------------------


def new_reference() -> str:
    """
    The reference for a checkout, minted here.

    It matters that this is server-side. The reference is the handle the app
    later hands to ``/billing/verify``, and one the *client* chose could be a
    reference it saw somewhere else. Because this one is generated alongside the
    metadata that names the payer, a reference and an owner are decided in the
    same breath.
    """
    return f"als_{uuid.uuid4().hex}"


# --- Metadata -----------------------------------------------------------------

#: Kora caps metadata at five fields with names of at most twenty characters.
#: Paystack had no such limit, and the old checkout used a nested
#: ``custom_fields`` array that Kora rejects outright — so the payload is built
#: here, once, rather than assembled at the call site.
_MAX_METADATA_FIELDS = 5
_MAX_KEY_LENGTH = 20


def build_metadata(*, user_id: str, tier: str, plan_name: str) -> dict[str, str]:
    """
    What travels with the charge, and comes back on the webhook.

    ``user_id`` is the field that matters: it is what ties a payment to a
    student without having to guess from an email most accounts do not have.
    """
    metadata = {
        "user_id": user_id,
        "tier": tier,
        "plan_name": plan_name,
    }

    trimmed = {
        key[:_MAX_KEY_LENGTH]: str(value)[:500]
        for key, value in list(metadata.items())[:_MAX_METADATA_FIELDS]
    }
    return trimmed


# --- Calls --------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.kora_secret_key}"}


async def initialize_transaction(
    client: httpx.AsyncClient,
    *,
    email: str,
    name: str,
    amount_kes: int,
    reference: str,
    metadata: dict,
    redirect_url: str | None = None,
    notification_url: str | None = None,
    narration: str | None = None,
) -> Checkout:
    """
    Opens a payment page for one student and one plan.

    This is what a shared payment link cannot do. That link is the same page for
    everybody, so the charge it produces carries no user id — leaving the server
    to guess who paid from whatever was typed into the form. Initialising here
    attaches ``metadata.user_id`` at the point where the caller's token has
    already proved who they are, so both the webhook and ``/billing/verify`` can
    credit the payment without guessing.
    """
    if not settings.kora_secret_key:
        raise AppError("Payments are not configured on this server.")

    payload: dict = {
        # Major unit. Kora charges exactly what is sent here — no multiplier.
        "amount": amount_kes,
        "currency": CURRENCY,
        "reference": reference,
        "customer": {"email": email, "name": name or "ALS student"},
        "metadata": metadata,
        "channels": CHANNELS,
        "default_channel": DEFAULT_CHANNEL,
        # False means the student pays the plan price and nothing more, with
        # Kora's fee coming out of it. True would add the fee on top, so the
        # M-Pesa prompt would ask for more than the pricing card promised.
        "merchant_bears_cost": True,
    }

    if narration:
        payload["narration"] = narration
    if redirect_url:
        payload["redirect_url"] = redirect_url
    if notification_url:
        # Set per charge rather than only in the dashboard, so a staging
        # deployment cannot quietly receive production's webhooks.
        payload["notification_url"] = notification_url

    try:
        response = await client.post(INITIALIZE_URL, json=payload, headers=_headers())
    except httpx.HTTPError as error:
        log.warning("kora_initialize_unreachable", reference=reference, error=str(error))
        raise AppError("We could not reach the payment provider. Try again shortly.") from None

    if response.status_code >= 400:
        log.warning(
            "kora_initialize_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("We could not start that payment. Try again shortly.")

    body = response.json().get("data") or {}
    url = body.get("checkout_url")

    if not url:
        log.error("kora_initialize_no_url", reference=reference, body=response.text[:400])
        raise AppError("We could not start that payment. Try again shortly.")

    return Checkout(
        checkout_url=url,
        # Read back rather than assumed, so the app verifies whatever was
        # actually opened.
        reference=body.get("reference", reference),
    )


def charge_from_data(data: dict, *, fallback_reference: str = "") -> Charge:
    """
    One shape for Kora's transaction object, wherever it came from.

    The verify response and the webhook payload describe the same thing with
    the same field names, so both go through here. Two hand-written mappings
    would be two chances to read the amount differently — which is how a webhook
    and a manual reconcile end up disagreeing about what a student paid.
    """
    customer = data.get("customer") or {}

    return Charge(
        reference=data.get("reference") or fallback_reference,
        status=data.get("status", "failed"),
        amount_kes=to_shillings(data.get("amount")),
        # Kora calls it `payment_method`; older payloads say `channel`.
        channel=data.get("payment_method") or data.get("channel") or "",
        email=customer.get("email", ""),
        metadata=data.get("metadata") or {},
    )


async def verify_transaction(client: httpx.AsyncClient, reference: str) -> Charge:
    """
    Asks Kora what actually happened to a reference.

    Called rather than trusting the client's word. The app writes an unverified
    subscription when a student says they paid; this is the call that turns that
    claim into a fact, and it is the only thing that should.
    """
    if not settings.kora_secret_key:
        raise AppError("Payments are not configured on this server.")

    try:
        response = await client.get(f"{VERIFY_URL}/{reference}", headers=_headers())
    except httpx.HTTPError as error:
        log.warning("kora_verify_unreachable", reference=reference, error=str(error))
        raise AppError("We could not reach the payment provider.", status_code=502) from None

    if response.status_code >= 400:
        log.warning(
            "kora_verify_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("That payment could not be verified.", status_code=402)

    return charge_from_data(response.json().get("data") or {}, fallback_reference=reference)
