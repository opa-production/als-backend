"""
M-Pesa, direct from Safaricom — Daraja STK Push.

The default way a student pays, because it is the cheapest. Kora and Paystack
both take a percentage for standing between us and the same M-Pesa transaction;
Daraja is Safaricom's own API and the only fee is Safaricom's own. On a KES 150
plan the difference is most of the margin.

**The shape is not a checkout page.** Every other provider here returns a URL to
open. Daraja pushes a PIN prompt to a phone: the student types their number in
the app, taps pay, and their handset rings. There is nothing to redirect to,
which is why `Checkout` in this module carries no URL and the app polls instead.

Three things about this API are unlike the others and each is a way to lose
money if missed:

1. **The callback is unauthenticated and unsigned.** Safaricom posts a plain
   JSON body to whatever URL was given, with no secret, no signature and no
   mutual TLS. Anyone who can reach the endpoint can post a "payment succeeded"
   for any `CheckoutRequestID` they can guess. So the callback is treated as a
   *hint that something happened*, never as the fact of payment — see
   `confirm`, which asks Safaricom directly before a shilling is credited.

2. **The amount is ours, not the student's.** Unlike a paybill number typed into
   the M-Pesa menu, an STK push names the amount in the request, so a successful
   push is a payment of exactly what the server asked for. That removes the
   whole class of "paid KES 10, claimed Synapse" problems the other adapters
   have to defend against by resolving tier from amount.

3. **`ResultCode: 0` on the query is the only success.** Everything else — the
   student cancelled, the PIN timed out, insufficient balance, a wrong PIN — is
   a distinct code and all of them mean no money moved.

Per Safaricom's published contract:
https://developer.safaricom.co.ke/APIs/MpesaExpressSimulate
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import structlog

from app.core.clock import now as utc_now
from app.core.config import settings
from app.core.errors import AppError

log = structlog.get_logger()

SANDBOX_ROOT = "https://sandbox.safaricom.co.ke"
PRODUCTION_ROOT = "https://api.safaricom.co.ke"

CURRENCY = "KES"

#: Safaricom's own timestamp format, and it is also half of the password.
#: Local Nairobi time, not UTC — the API rejects a timestamp that disagrees with
#: the one baked into the password, and it reads both as EAT.
_STAMP = "%Y%m%d%H%M%S"
NAIROBI_OFFSET = timedelta(hours=3)

#: The only ResultCode that means money moved.
SUCCESS_CODE = 0

#: What a student is told for the codes worth explaining. Anything else falls
#: back to a general line — a student cannot act on "503.01.00000001" and does
#: not need to see it, while the log keeps every one.
RESULT_MESSAGES = {
    1: "You do not have enough M-Pesa balance for that.",
    1031: "You cancelled the payment.",
    1032: "You cancelled the payment.",
    1037: "The prompt timed out. Check your phone is on and try again.",
    2001: "That PIN was wrong. Try again.",
}


@dataclass(frozen=True)
class Push:
    """An STK prompt that is now ringing on somebody's phone."""

    #: Safaricom's handle for this prompt. The only thing that ties a later
    #: callback back to the row this service created, so it is stored and every
    #: callback is matched against it.
    checkout_request_id: str
    merchant_request_id: str
    #: Our own reference, minted here and sent as `AccountReference`.
    reference: str
    #: Safaricom's own wording of "check your phone", which is worth showing
    #: verbatim: it is what the student is about to see on their handset.
    customer_message: str


@dataclass(frozen=True)
class Result:
    """What Safaricom says happened to a prompt."""

    #: 0 is paid. Anything else is not, and `message` says why where we know.
    result_code: int
    message: str
    #: True only while the student still has the prompt on screen. Distinct from
    #: failure: the app keeps waiting rather than offering another provider.
    pending: bool = False

    @property
    def paid(self) -> bool:
        return self.result_code == SUCCESS_CODE


def configured() -> bool:
    """
    Whether a push can be attempted at all.

    Checked before choosing a provider rather than discovered by a failed
    request: a box with no Daraja credentials should quietly use the fallback,
    not put a student through a request that cannot work.
    """
    return bool(
        settings.daraja_consumer_key
        and settings.daraja_consumer_secret
        and settings.daraja_shortcode
        and settings.daraja_passkey
    )


def _root() -> str:
    return (
        PRODUCTION_ROOT
        if settings.daraja_environment == "production"
        else SANDBOX_ROOT
    )


# --- Phone numbers ------------------------------------------------------------

_DIGITS = re.compile(r"\D")


def normalise_phone(raw: str | None) -> str:
    """
    A Kenyan mobile number in the only format Daraja accepts: ``2547XXXXXXXX``.

    Students type all of these and mean the same phone: ``0712345678``,
    ``+254712345678``, ``254 712 345 678``, ``712345678``. Daraja accepts
    exactly one of them and answers the rest with an error that names a field
    rather than the problem, so the normalising happens here and the app is free
    to let people type what they like.

    Safaricom is 07xx and 01xx. Both are accepted; anything else is refused with
    a message a student can act on, because a push to a number that cannot
    receive it fails minutes later with nothing useful attached.
    """
    digits = _DIGITS.sub("", raw or "")

    if not digits:
        raise AppError("Enter the M-Pesa number to pay from.")

    # 0712345678 / 0112345678 → 712345678 / 112345678
    if digits.startswith("0"):
        digits = digits[1:]
    # 254712345678 → 712345678
    elif digits.startswith("254"):
        digits = digits[3:]

    if len(digits) != 9 or digits[0] not in ("7", "1"):
        raise AppError(
            "That does not look like a Safaricom number. "
            "It should start 07 or 01."
        )

    return f"254{digits}"


# --- Credentials --------------------------------------------------------------


@dataclass
class _Token:
    value: str
    expires_at: datetime


#: Cached because Daraja's token lasts an hour and its OAuth endpoint is rate
#: limited. Minting one per payment is a second request in front of every
#: student's checkout, and a burst of them is how the endpoint starts refusing.
#:
#: Module-level rather than on a client: the credentials are per deployment, not
#: per request, and the worker and the API each keep their own quite happily.
_token: _Token | None = None

#: Renewed early. A token that expires between being read and being used
#: produces a 401 on a payment the student has already tapped through.
_TOKEN_MARGIN = timedelta(minutes=5)


def reset_token_cache() -> None:
    """Drop the cached token. For tests, and for a credentials change."""
    global _token
    _token = None


async def access_token(client: httpx.AsyncClient) -> str:
    global _token

    now = utc_now()
    if _token is not None and _token.expires_at - _TOKEN_MARGIN > now:
        return _token.value

    if not configured():
        raise AppError("M-Pesa is not configured on this server.")

    try:
        response = await client.get(
            f"{_root()}/oauth/v1/generate?grant_type=client_credentials",
            auth=(settings.daraja_consumer_key, settings.daraja_consumer_secret),
        )
    except httpx.HTTPError as error:
        log.warning(
            "daraja_token_unreachable",
            host=_root(),
            environment=settings.daraja_environment,
            error=str(error),
        )
        raise AppError("We could not reach M-Pesa. Try again shortly.") from None

    if response.status_code >= 400:
        log.warning(
            "daraja_token_failed",
            status=response.status_code,
            body=response.text[:300],
        )
        raise AppError("We could not reach M-Pesa. Try again shortly.")

    body = response.json()
    value = body.get("access_token")
    if not value:
        log.error("daraja_token_missing", body=response.text[:300])
        raise AppError("We could not reach M-Pesa. Try again shortly.")

    # Safaricom returns seconds as a string on some responses and a number on
    # others. An unreadable value falls back to a short life rather than
    # raising: a token cached too briefly costs one extra request, a token
    # cached too long costs a failed payment.
    try:
        lifetime = int(float(body.get("expires_in", 3599)))
    except (TypeError, ValueError):
        lifetime = 600

    _token = _Token(value=value, expires_at=now + timedelta(seconds=lifetime))
    return value


def _password(timestamp: str) -> str:
    """
    Daraja's per-request password: base64 of shortcode + passkey + timestamp.

    The timestamp here has to be the *same string* sent as ``Timestamp``. Two
    calls to `now()` either side of a second boundary produce a password that
    does not match the timestamp beside it, and Daraja answers that with an
    invalid-credentials error that looks like a wrong passkey.
    """
    raw = f"{settings.daraja_shortcode}{settings.daraja_passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def _timestamp() -> str:
    return (utc_now() + NAIROBI_OFFSET).strftime(_STAMP)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


#: Retried once, and only for failures where the request provably never left
#: this machine.
#:
#: `ConnectError` covers DNS — "[Errno -3] Temporary failure in name resolution"
#: arrives as one — and Safaricom's resolver is flaky enough that a single retry
#: turns most of those into a successful payment rather than a fall back to a
#: provider that charges us a percentage.
#:
#: A read timeout is deliberately **not** in here. That means the request was
#: sent and the answer was lost, so a retry may put a *second* PIN prompt on a
#: student's phone for one plan. Falling back is the cheap mistake; double
#: prompting is not.
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout)


async def _post(
    client: httpx.AsyncClient, path: str, payload: dict, token: str, *, reference: str
) -> httpx.Response:
    """
    One Daraja call, with a single retry on a connection that never opened.

    The host and environment go into the log because without them a failure is
    unattributable: "name resolution failed" says nothing about *which* name,
    and the answer — sandbox or production — is the first thing worth knowing.
    """
    url = f"{_root()}{path}"
    last: Exception | None = None

    for attempt in (1, 2):
        try:
            return await client.post(url, json=payload, headers=_headers(token))
        except _RETRYABLE as error:
            last = error
            log.warning(
                "daraja_connect_failed",
                reference=reference,
                attempt=attempt,
                host=_root(),
                environment=settings.daraja_environment,
                error=str(error),
            )
        except httpx.HTTPError as error:
            # Sent, and the answer lost. Not retried — see `_RETRYABLE`.
            log.warning(
                "daraja_unreachable",
                reference=reference,
                host=_root(),
                environment=settings.daraja_environment,
                error=str(error),
            )
            raise AppError("We could not reach M-Pesa. Try again shortly.") from None

    raise AppError("We could not reach M-Pesa. Try again shortly.") from last


# --- Pushing ------------------------------------------------------------------


async def push_stk(
    client: httpx.AsyncClient,
    *,
    phone: str,
    amount_kes: int,
    reference: str,
    description: str,
    callback_url: str,
) -> Push:
    """
    Rings a student's phone with a PIN prompt.

    ``amount_kes`` comes from the server's plan table and is never taken from
    the request — it is the single most important thing about this call. Daraja
    charges exactly what is sent, so a client-supplied amount is a client-chosen
    price.

    Whole shillings only. Daraja rejects a decimal amount, and every plan here
    is priced in whole shillings anyway.
    """
    if not configured():
        raise AppError("M-Pesa is not configured on this server.")
    if not callback_url:
        # Without it the payment completes and nothing is ever told about it.
        # Refusing now is far better than taking money we cannot credit.
        raise AppError("M-Pesa is not configured on this server.")

    token = await access_token(client)
    timestamp = _timestamp()

    payload = {
        "BusinessShortCode": settings.daraja_shortcode,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": settings.daraja_transaction_type,
        "Amount": int(amount_kes),
        "PartyA": phone,
        # Where the money lands, and the one field that differs between the two
        # kinds of account:
        #
        #   Paybill    BusinessShortCode and PartyB are the same paybill number.
        #   Buy Goods  BusinessShortCode is the *store* (head office) number —
        #              which is also what the passkey is issued against, and so
        #              what `_password` hashes — while PartyB is the *till*.
        #
        # Swapping those two is rejected with an error naming a field rather
        # than the mismatch, which is a slow afternoon. Blank falls back to the
        # shortcode, so a paybill deployment sets one number and not two.
        "PartyB": settings.daraja_party_b or settings.daraja_shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        # What the student sees on their M-Pesa statement, and what comes back
        # on the callback. Our own reference, so a statement line can be traced
        # to a row.
        "AccountReference": reference[:12],
        "TransactionDesc": description[:13],
    }

    response = await _post(
        client, "/mpesa/stkpush/v1/processrequest", payload, token, reference=reference
    )

    if response.status_code >= 400:
        log.warning(
            "daraja_push_failed",
            reference=reference,
            status=response.status_code,
            body=response.text[:400],
        )
        raise AppError("We could not start that M-Pesa payment.")

    body = response.json()

    # Daraja answers 200 with a non-zero ResponseCode for a request it accepted
    # the shape of but refused — an invalid shortcode, a number it will not push
    # to. Treating 200 as success is how a student is told to check a phone that
    # is never going to ring.
    if str(body.get("ResponseCode", "")) != "0":
        log.warning(
            "daraja_push_rejected",
            reference=reference,
            code=body.get("ResponseCode"),
            description=body.get("ResponseDescription"),
        )
        raise AppError("We could not start that M-Pesa payment.")

    checkout_request_id = body.get("CheckoutRequestID")
    if not checkout_request_id:
        log.error("daraja_push_no_id", reference=reference, body=response.text[:300])
        raise AppError("We could not start that M-Pesa payment.")

    return Push(
        checkout_request_id=checkout_request_id,
        merchant_request_id=body.get("MerchantRequestID", ""),
        reference=reference,
        customer_message=body.get("CustomerMessage")
        or "Check your phone for the M-Pesa prompt.",
    )


# --- Confirming ---------------------------------------------------------------


async def confirm(client: httpx.AsyncClient, checkout_request_id: str) -> Result:
    """
    Asks Safaricom what happened, rather than believing anyone else.

    **This is the security boundary of the whole M-Pesa path.** The callback
    Safaricom posts is unsigned and unauthenticated: there is no secret, no
    signature header and no way to distinguish it from a request somebody else
    made up. Crediting a plan from that body would mean anyone who can reach the
    endpoint and guess a `CheckoutRequestID` gets a free subscription.

    So nothing is credited from a callback. The callback wakes this up, and this
    asks Daraja directly. The answer to *that* is the fact.

    It is also what makes the app's polling work at all: a student on a bad
    connection may never have the callback delivered, and this is the same
    question asked from the other end.
    """
    if not configured():
        raise AppError("M-Pesa is not configured on this server.")

    token = await access_token(client)
    timestamp = _timestamp()

    payload = {
        "BusinessShortCode": settings.daraja_shortcode,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }

    try:
        response = await _post(
            client,
            "/mpesa/stkpushquery/v1/query",
            payload,
            token,
            reference=checkout_request_id,
        )
    except AppError:
        # Unknown, not failed. A network blip must never read as "they did not
        # pay" — the student may well have, and the next poll will find out.
        return Result(result_code=-1, message="Still waiting for M-Pesa.", pending=True)

    body = {}
    try:
        body = response.json()
    except ValueError:
        pass

    # While the prompt is still on the handset, Daraja answers 500 with
    # `500.001.1001` — "transaction is being processed". That is *pending*, and
    # reading it as a failure is what makes a student who is mid-PIN be told
    # their payment did not work.
    code = str(body.get("errorCode") or "")
    if response.status_code >= 400:
        if code.endswith("1001") or "processed" in str(body.get("errorMessage", "")):
            return Result(
                result_code=-1, message="Still waiting for M-Pesa.", pending=True
            )
        log.warning(
            "daraja_query_failed",
            status=response.status_code,
            body=response.text[:300],
        )
        return Result(result_code=-1, message="Still waiting for M-Pesa.", pending=True)

    try:
        result_code = int(body.get("ResultCode", -1))
    except (TypeError, ValueError):
        result_code = -1

    if result_code == SUCCESS_CODE:
        return Result(result_code=0, message="Paid.")

    if result_code < 0:
        return Result(result_code=-1, message="Still waiting for M-Pesa.", pending=True)

    return Result(
        result_code=result_code,
        message=RESULT_MESSAGES.get(
            result_code, "That payment did not go through. You have not been charged."
        ),
    )


# --- Callbacks ----------------------------------------------------------------


@dataclass(frozen=True)
class CallbackHint:
    """
    What a callback body claims.

    Named a *hint* on purpose, and the name is the documentation: nothing on
    this object is trusted. The only field acted on is `checkout_request_id`,
    and only to look up a row this service created and then ask Safaricom what
    really happened.
    """

    checkout_request_id: str
    result_code: int
    #: The M-Pesa code on the student's SMS — "SFK4H8ZQ2L". Stored for support,
    #: because it is the thing a student quotes when something goes wrong. Never
    #: used to decide anything.
    receipt: str = ""


def read_callback(body: dict) -> CallbackHint | None:
    """
    Pulls the two useful fields out of Safaricom's callback envelope.

    Returns ``None`` for anything that is not shaped like one, so a stray POST
    is discarded quietly rather than raising into the logs — this endpoint is
    open to the internet and will be probed.
    """
    stk = ((body or {}).get("Body") or {}).get("stkCallback") or {}

    checkout_request_id = stk.get("CheckoutRequestID")
    if not checkout_request_id:
        return None

    try:
        result_code = int(stk.get("ResultCode", -1))
    except (TypeError, ValueError):
        result_code = -1

    receipt = ""
    for item in ((stk.get("CallbackMetadata") or {}).get("Item") or []):
        if item.get("Name") == "MpesaReceiptNumber":
            receipt = str(item.get("Value") or "")[:32]
            break

    return CallbackHint(
        checkout_request_id=str(checkout_request_id),
        result_code=result_code,
        receipt=receipt,
    )
