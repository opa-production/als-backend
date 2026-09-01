"""
Paystack — card payments, on an account this app does not have to itself.

Cards are the one thing neither Daraja nor Kora covers in Kenya, so this exists
for exactly that: a student paying with a Visa or Mastercard.

**This adapter is built to share a Paystack business with another product**,
which is the situation here, and that constraint shapes every decision below.

Two dashboard-level settings belong to whoever set them up first, and neither
can be changed without affecting the other app:

1. **The callback URL.** Bypassed, and easily: `callback_url` is accepted per
   transaction on `transaction/initialize` and overrides the dashboard default
   for that transaction only. The other app's URL stays where it is and never
   sees an ALS payment.

2. **The webhook URL.** *Cannot* be bypassed. Paystack posts every event on the
   account to the one URL on the dashboard, and there is no per-transaction
   override. On a shared account that URL is the other app's, so **ALS will
   never receive a Paystack webhook** and must not be built as though it will.

So this integration is a **pull**, not a push. `/billing/verify` asks Paystack
what happened when the student returns, and a sweep on the worker catches
anything that never came back — a closed tab, a dead battery, a browser that
lost the redirect. That is strictly more reliable than a webhook anyway: a
webhook is one delivery that a deploy or a dropped connection loses for ever,
while a question can be asked again.

`verify_signature` is still here and still correct, for the day this moves to
its own account, or if the other app is ever made to forward events. It is
written so that an event about *the other app's* transaction is ignored rather
than credited — see `is_ours`.

Three differences from Kora, each silent when wrong:

* **Amount is the minor unit.** 350 shillings is ``35000``. Kora takes the
  major unit, so a straight copy of that adapter undercharges by a hundred.
* **The signature covers the whole raw body**, HMAC-SHA512 with the secret key.
  Kora signs only ``data``, with SHA-256. Neither digest validates the other.
* **The link is ``authorization_url``**, not ``checkout_url``.

Per Paystack's published contract: https://paystack.com/docs/api/transaction/
"""

from __future__ import annotations

import hashlib
import hmac

import httpx
import structlog

from app.core.config import settings
from app.core.errors import AppError
from app.services.kora import Charge, Checkout

log = structlog.get_logger()

API_ROOT = "https://api.paystack.co"
INITIALIZE_URL = f"{API_ROOT}/transaction/initialize"
VERIFY_URL = f"{API_ROOT}/transaction/verify"

CURRENCY = "KES"

#: Cards only. Mobile money on this account would be the same M-Pesa payment
#: Daraja already takes directly, with a processor's percentage on top — and
#: offering it here would quietly route students away from the cheap path.
CHANNELS = ["card"]

SUCCESS = "success"

#: Paystack prices in the minor unit. See the module docstring: this is the
#: multiplier that a copy of the Kora adapter would be missing.
MINOR_UNITS = 100


def configured() -> bool:
    return bool(settings.paystack_secret_key)


def to_shillings(value: object) -> int:
    """
    Paystack's amount, back in whole shillings.

    Divides by 100, which is the exact opposite of what the Kora adapter does
    and the reason both functions have the same name in different modules
    rather than one shared helper — a single `to_shillings` used by both would
    have to know which provider it was called for, and getting that wrong is a
    hundredfold error in one direction or the other.
    """
    if value is None:
        return 0
    try:
        return int(round(float(value) / MINOR_UNITS))
    except (TypeError, ValueError):
        log.warning("paystack_unreadable_amount", value=repr(value)[:80])
        return 0


# --- Signatures ---------------------------------------------------------------


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    Confirms a webhook really came from Paystack.

    The whole raw body, HMAC-SHA512, keyed with the secret key — not the
    ``data`` object and not SHA-256, which is Kora's convention next door. The
    two are easy to transplant and each produces a digest that never matches.

    Not currently reachable in the shared-account setup: Paystack posts to the
    dashboard URL, which is the other app's. Kept because it is the piece that
    would be wrong if written in a hurry later, and because nothing here should
    ever accept an unsigned webhook if one does start arriving.
    """
    secret = settings.paystack_secret_key
    if not secret or not signature:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def is_ours(charge: Charge) -> bool:
    """
    Whether a Paystack transaction belongs to this product at all.

    **The rule that makes a shared account safe.** Every event on the business
    is delivered to whichever webhook is configured, including the other app's
    transactions. Crediting on amount alone would mean a KES 350 payment made by
    somebody in a completely different product activating a plan here.

    Two independent markers, both set by this service at checkout: a reference
    minted with our own prefix, and ``metadata.user_id`` naming an ALS account.
    Both must be present. Neither is guessable and the other app sets neither.
    """
    reference = (charge.reference or "").strip()
    if not reference.startswith(REFERENCE_PREFIX):
        return False

    return bool((charge.metadata or {}).get("user_id"))


#: The prefix every reference this service mints carries — see
#: `kora.new_reference`, which is shared across providers precisely so that one
#: rule identifies our transactions on any dashboard we are a guest on.
REFERENCE_PREFIX = "als_"


# --- Calls --------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.paystack_secret_key}",
        "Content-Type": "application/json",
    }


def _charge_from_data(data: dict, *, fallback_reference: str = "") -> Charge:
    """
    Paystack's transaction, in the shape everything downstream already speaks.

    The same `Charge` the Kora adapter returns, so `tier_from_charge`,
    `record_payment` and `assert_charge_belongs_to` needed no change when a
    third provider arrived. Which provider took the money is an implementation
    detail of this module.
    """
    customer = data.get("customer") or {}

    return Charge(
        reference=data.get("reference") or fallback_reference,
        status=data.get("status", "failed"),
        # Divided, not multiplied. The one line where a copied Kora adapter
        # would be wrong by a factor of ten thousand.
        amount_kes=to_shillings(data.get("amount")),
        channel=data.get("channel") or "card",
        email=customer.get("email", ""),
        metadata=data.get("metadata") or {},
    )


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
    Opens a card checkout for one student and one plan.

    ``callback_url`` is passed **per transaction**, which is the whole reason
    this works on a borrowed account: it overrides the dashboard's default for
    this transaction only, so the student comes back to ALS while the other
    app's configured URL is left exactly as it is.

    ``metadata.user_id`` is written from the caller's token, so the transaction
    knows whose it is before the student has typed a card number. On a shared
    account that is not merely convenient — it is half of `is_ours`, and the
    reason another product's payment can never be mistaken for one of ours.
    """
    if not configured():
        raise AppError("Card payments are not configured on this server.")

    payload: dict = {
        "email": email,
        # Minor unit. See the module docstring — the single most expensive
        # line in this file to get wrong.
        "amount": int(amount_kes) * MINOR_UNITS,
        "currency": CURRENCY,
        "reference": reference,
        "metadata": metadata,
        "channels": CHANNELS,
    }
    if callback_url:
        payload["callback_url"] = callback_url

    try:
        response = await client.post(
            INITIALIZE_URL, json=payload, headers=_headers()
        )
    except httpx.HTTPError as error:
        log.warning(
            "paystack_initialize_unreachable", reference=reference, error=str(error)
        )
        raise AppError(
            "We could not reach the card processor. Try again shortly."
        ) from None

    if response.status_code >= 400:
        log.warning(
            "paystack_initialize_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("We could not start that card payment. Try again shortly.")

    body = response.json()
    if not body.get("status"):
        # Paystack answers 200 with `status: false` for a request it accepted
        # the shape of and then refused — an unsupported currency on the
        # account, a duplicate reference. Reading 200 as success sends a student
        # to a URL that is not there.
        log.warning(
            "paystack_initialize_rejected",
            reference=reference,
            message=body.get("message"),
        )
        raise AppError("We could not start that card payment. Try again shortly.")

    data = body.get("data") or {}
    url = data.get("authorization_url")

    if not url:
        log.error(
            "paystack_initialize_no_url", reference=reference, body=response.text[:400]
        )
        raise AppError("We could not start that card payment. Try again shortly.")

    return Checkout(
        checkout_url=url,
        reference=data.get("reference", reference),
    )


async def verify_transaction(client: httpx.AsyncClient, reference: str) -> Charge:
    """
    Asks Paystack what actually happened to a reference.

    **The primary settlement path here, not a backstop.** With no webhook
    reaching this service, this call and the sweep that also uses it are the
    only ways a card payment ever becomes a subscription.

    A `404` is not an error worth raising over: on a shared account it means the
    reference belongs to the other app, or to nobody. It comes back as a failed
    charge so the caller can decline it without a stack trace.
    """
    if not configured():
        raise AppError("Card payments are not configured on this server.")

    try:
        response = await client.get(
            f"{VERIFY_URL}/{reference}", headers=_headers()
        )
    except httpx.HTTPError as error:
        log.warning("paystack_verify_unreachable", reference=reference, error=str(error))
        raise AppError(
            "We could not reach the card processor.", status_code=502
        ) from None

    if response.status_code == 404:
        return Charge(
            reference=reference,
            status="failed",
            amount_kes=0,
            channel="card",
            email="",
            metadata={},
        )

    if response.status_code >= 400:
        log.warning(
            "paystack_verify_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("That payment could not be verified.", status_code=402)

    body = response.json()
    return _charge_from_data(body.get("data") or {}, fallback_reference=reference)
