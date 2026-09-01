"""
M-Pesa, direct from Safaricom.

Two halves, and the second is the one that matters.

The first is that a payment works: a prompt goes out with the right amount, the
student pays, the plan turns on, and it turns on exactly once however many ways
the news arrives.

The second is that **the callback endpoint cannot be used to buy a plan**.
Safaricom signs nothing — there is no secret, no signature header, no mutual
TLS — so that endpoint is a public URL anybody can POST to. Every test under
"Forgery" describes something a person with curl would actually try.
"""

import json

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_http_client
from app.core.config import settings
from app.core.errors import AppError
from app.main import app
from app.models.billing import Payment, Subscription
from app.services import daraja
from app.services.plans import Tier, plan_for
from tests.conftest import OTHER_PHONE, sign_in

CHECKOUT_ID = "ws_CO_01092026120000123456"
MERCHANT_ID = "29115-34620561-1"
RECEIPT = "SFK4H8ZQ2L"


# --- Phone numbers ------------------------------------------------------------
#
# Daraja accepts exactly one format and answers every other with an error naming
# a field rather than the problem. Students type all of these.


@pytest.mark.parametrize(
    "typed",
    [
        "0712345678",
        "+254712345678",
        "254712345678",
        "712345678",
        "0712 345 678",
        " 0712-345-678 ",
    ],
)
def test_every_way_a_student_writes_their_number_works(typed):
    assert daraja.normalise_phone(typed) == "254712345678"


def test_the_newer_01_range_is_accepted():
    """Safaricom has issued 01 numbers for years. Refusing them refuses money."""
    assert daraja.normalise_phone("0112345678") == "254112345678"


@pytest.mark.parametrize(
    "typed", ["", None, "12345", "0812345678", "0712345", "not a number"]
)
def test_a_number_that_cannot_receive_a_prompt_is_refused_early(typed):
    """
    A push to an unusable number fails minutes later with nothing attached, so
    the refusal happens before the request and says what is wrong.
    """
    with pytest.raises(AppError):
        daraja.normalise_phone(typed)


# --- The provider stub --------------------------------------------------------


class FakeDaraja:
    """
    Safaricom, as far as this service can tell.

    Mocked at the transport rather than at `daraja.push_stk`, so the request
    that is actually built — the password, the timestamp, the amount, the
    callback URL — is the thing under test. Stubbing the function would assert
    nothing about any of it.
    """

    def __init__(self):
        self.push_payload: dict | None = None
        self.query_payload: dict | None = None
        #: What `stkpushquery` answers. 0 is paid; 1032 is "student cancelled".
        self.result_code = 0
        #: Set to make the query behave as it does while the prompt is still on
        #: the handset: HTTP 500 with a "being processed" error code.
        self.still_processing = False
        #: Set to refuse the push itself, the way Daraja does for a bad
        #: shortcode — 200 with a non-zero ResponseCode.
        self.push_response_code = "0"
        self.push_status = 200
        self.queries = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "/oauth/v1/generate" in url:
            return httpx.Response(
                200, json={"access_token": "tok-abc", "expires_in": "3599"}
            )

        if "/stkpush/v1/processrequest" in url:
            self.push_payload = json.loads(request.content)
            if self.push_status >= 400:
                return httpx.Response(self.push_status, json={"errorMessage": "no"})
            return httpx.Response(
                200,
                json={
                    "MerchantRequestID": MERCHANT_ID,
                    "CheckoutRequestID": CHECKOUT_ID,
                    "ResponseCode": self.push_response_code,
                    "ResponseDescription": "Success. Request accepted",
                    "CustomerMessage": "Success. Request accepted for processing",
                },
            )

        if "/stkpushquery/v1/query" in url:
            self.queries += 1
            self.query_payload = json.loads(request.content)
            if self.still_processing:
                return httpx.Response(
                    500,
                    json={
                        "errorCode": "500.001.1001",
                        "errorMessage": "The transaction is being processed",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ResponseCode": "0",
                    "ResultCode": str(self.result_code),
                    "ResultDesc": "ok",
                },
            )

        return httpx.Response(404)


@pytest.fixture
def mpesa(client, monkeypatch):
    monkeypatch.setattr(settings, "daraja_consumer_key", "ck")
    monkeypatch.setattr(settings, "daraja_consumer_secret", "cs")
    monkeypatch.setattr(settings, "daraja_shortcode", "174379")
    monkeypatch.setattr(settings, "daraja_passkey", "passkey")
    monkeypatch.setattr(settings, "daraja_environment", "sandbox")
    monkeypatch.setattr(settings, "daraja_callback_override", "https://als.test/cb")
    # The token is cached module-wide and outlives a test otherwise, which
    # would let one test's credentials authenticate another's requests.
    daraja.reset_token_cache()

    fake = FakeDaraja()
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    app.dependency_overrides[get_http_client] = lambda: outbound
    yield fake
    daraja.reset_token_cache()


async def _pay(client, headers, *, tier="pro", phone="0712345678"):
    return await client.post(
        "/api/v1/billing/mpesa",
        json={"tier": tier, "phone": phone},
        headers=headers,
    )


def _callback(*, checkout_id=CHECKOUT_ID, result_code=0, receipt=RECEIPT) -> dict:
    """A callback body shaped exactly as Safaricom posts one."""
    body = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": MERCHANT_ID,
                "CheckoutRequestID": checkout_id,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully.",
            }
        }
    }
    if result_code == 0:
        body["Body"]["stkCallback"]["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": 350.0},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "PhoneNumber", "Value": 254712345678},
            ]
        }
    return body


async def _payment(client, reference) -> Payment:
    async with client.sessions() as session:
        return await session.scalar(
            select(Payment).where(Payment.reference == reference)
        )


# --- Pushing ------------------------------------------------------------------


async def test_paying_rings_the_students_phone(client, mpesa):
    headers, _ = await sign_in(client)

    response = await _pay(client, headers)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "stk"
    assert body["provider"] == "daraja"
    assert body["phone"] == "254712345678"
    assert body["amount_ksh"] == plan_for(Tier.PRO).price_ksh
    assert body["reference"]


async def test_the_amount_comes_from_the_plan_table_not_the_request(client, mpesa):
    """
    The single most important line in the whole M-Pesa path.

    Daraja charges exactly what is sent. A price the client can influence is a
    price the client can choose, so the request carries a tier and nothing else
    about money — and even a request that tries to name one is ignored.
    """
    headers, _ = await sign_in(client)

    await client.post(
        "/api/v1/billing/mpesa",
        json={"tier": "standard", "phone": "0712345678", "amount": 1, "amount_ksh": 1},
        headers=headers,
    )

    assert mpesa.push_payload["Amount"] == plan_for(Tier.STANDARD).price_ksh


async def test_the_push_is_addressed_and_signed_the_way_daraja_expects(client, mpesa):
    """
    The password is base64 of shortcode + passkey + timestamp, and it has to
    agree with the `Timestamp` sent beside it. Two clock reads either side of a
    second boundary produce a mismatch that Daraja reports as bad credentials,
    which looks like a wrong passkey and is not.
    """
    import base64

    headers, _ = await sign_in(client)
    await _pay(client, headers)

    payload = mpesa.push_payload
    expected = base64.b64encode(
        f"174379passkey{payload['Timestamp']}".encode()
    ).decode()

    assert payload["Password"] == expected
    assert payload["BusinessShortCode"] == "174379"
    assert payload["PartyB"] == "174379"
    assert payload["PhoneNumber"] == "254712345678"
    assert payload["CallBackURL"] == "https://als.test/cb"
    assert payload["TransactionType"] == "CustomerPayBillOnline"


async def test_a_plan_that_is_not_for_sale_cannot_be_pushed(client, mpesa):
    headers, _ = await sign_in(client)

    for tier in ("free", "trial", "expired", "nonsense"):
        response = await _pay(client, headers, tier=tier)
        assert response.status_code >= 400, tier

    assert mpesa.push_payload is None


async def test_paying_needs_a_token(client, mpesa):
    response = await client.post(
        "/api/v1/billing/mpesa", json={"tier": "pro", "phone": "0712345678"}
    )
    assert response.status_code == 401


async def test_a_pending_row_exists_before_the_student_can_answer(client, mpesa):
    """
    The row is the authorisation story of the callback endpoint. Written before
    the prompt can possibly be answered, so there is no window in which a
    genuine callback arrives with nothing to match it to.
    """
    headers, user_id = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    payment = await _payment(client, reference)

    assert payment is not None
    assert payment.user_id == user_id
    assert payment.provider == "daraja"
    assert payment.checkout_request_id == CHECKOUT_ID
    assert payment.status == "pending"
    assert payment.amount_kes == plan_for(Tier.PRO).price_ksh


# --- Getting paid -------------------------------------------------------------


async def test_a_completed_payment_turns_the_plan_on(client, mpesa):
    headers, _ = await sign_in(client)
    await _pay(client, headers)

    response = await client.post(
        "/api/v1/billing/mpesa/callback", json=_callback()
    )
    assert response.status_code == 200

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "pro"
    assert subscription["verified"] is True


async def test_polling_credits_the_plan_when_the_callback_never_arrives(client, mpesa):
    """
    The callback is one unauthenticated POST that a dropped connection loses for
    ever. Polling asks the same question from the other end, and it is the half
    that actually has to work.
    """
    headers, _ = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    status = await client.get(
        f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
    )

    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "success"
    assert body["pending"] is False
    assert body["subscription"]["tier"] == "pro"


async def test_a_prompt_still_on_screen_reads_as_pending_not_failed(client, mpesa):
    """
    While the student is typing their PIN, Daraja answers the query with HTTP
    500 and `500.001.1001`. Reading that as a failure tells somebody mid-payment
    that their payment did not work.
    """
    headers, _ = await sign_in(client)
    mpesa.still_processing = True
    reference = (await _pay(client, headers)).json()["reference"]

    body = (
        await client.get(
            f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
        )
    ).json()

    assert body["pending"] is True
    assert body["status"] == "pending"

    payment = await _payment(client, reference)
    assert payment.status == "pending", "a pending prompt must not be closed out"


async def test_a_cancelled_prompt_is_recorded_with_a_reason(client, mpesa):
    headers, _ = await sign_in(client)
    mpesa.result_code = 1032
    reference = (await _pay(client, headers)).json()["reference"]

    body = (
        await client.get(
            f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
        )
    ).json()

    assert body["status"] == "failed"
    assert body["pending"] is False
    assert "cancelled" in body["message"]

    payment = await _payment(client, reference)
    assert payment.status == "failed"

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "free"


async def test_the_reason_is_written_for_a_student(client, mpesa):
    """A result code is not a message. "1" means they are out of money."""
    headers, _ = await sign_in(client)
    mpesa.result_code = 1
    reference = (await _pay(client, headers)).json()["reference"]

    body = (
        await client.get(
            f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
        )
    ).json()

    assert "balance" in body["message"]
    assert "1" != body["message"]


# --- Paying twice -------------------------------------------------------------


async def test_the_callback_and_the_poll_credit_the_plan_once_between_them(
    client, mpesa
):
    """
    Both arrive, and they race — the app polls every few seconds while
    Safaricom posts. Crediting each would give sixty days for thirty days'
    money.
    """
    headers, user_id = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    await client.post("/api/v1/billing/mpesa/callback", json=_callback())
    first = await _expiry(client, user_id)

    await client.post("/api/v1/billing/mpesa/callback", json=_callback())
    await client.get(
        f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
    )
    await client.get(
        f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
    )

    assert await _expiry(client, user_id) == first


async def _expiry(client, user_id):
    async with client.sessions() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        return subscription.expires_at


async def test_safaricom_redelivering_a_callback_changes_nothing(client, mpesa):
    headers, user_id = await sign_in(client)
    await _pay(client, headers)

    for _ in range(5):
        response = await client.post(
            "/api/v1/billing/mpesa/callback", json=_callback()
        )
        # Always 200: a non-2xx has Safaricom retrying for hours over something
        # already recorded.
        assert response.status_code == 200

    async with client.sessions() as session:
        rows = (
            await session.scalars(
                select(Payment).where(Payment.user_id == user_id)
            )
        ).all()

    assert len([row for row in rows if row.status == "success"]) == 1


# --- Forgery ------------------------------------------------------------------
#
# The callback endpoint is a public, unauthenticated URL — Safaricom signs
# nothing. Everything here is something a person with curl would try.


async def test_a_callback_for_a_prompt_we_never_sent_buys_nothing(client, mpesa):
    """
    The first gate. An id this service never issued has no row, and no row means
    no account to credit — there is nothing else for an attacker to aim at.
    """
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/billing/mpesa/callback",
        json=_callback(checkout_id="ws_CO_MADE_UP_00000000"),
    )

    assert response.status_code == 200
    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "free"


async def test_a_forged_success_is_overruled_by_safaricom(client, mpesa):
    """
    **The security boundary of the whole M-Pesa path.**

    The attacker has a real `CheckoutRequestID` — their own, from a prompt they
    started and then cancelled — and posts a callback claiming ResultCode 0.
    The body is entirely under their control and there is no signature to fail.

    Nothing is credited from that body. The endpoint asks Safaricom what
    happened, Safaricom says the prompt was cancelled, and the plan stays off.
    """
    headers, _ = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    # They cancelled it. Safaricom knows.
    mpesa.result_code = 1032

    response = await client.post(
        "/api/v1/billing/mpesa/callback", json=_callback(result_code=0)
    )

    assert response.status_code == 200
    assert mpesa.queries >= 1, "the callback must be confirmed, never believed"

    payment = await _payment(client, reference)
    assert payment.status == "failed"

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "free"


async def test_junk_posted_at_the_callback_is_absorbed(client, mpesa):
    """This endpoint is open to the internet and will be probed."""
    for body in (
        {},
        {"Body": {}},
        {"Body": {"stkCallback": {}}},
        {"hello": "world"},
        [],
    ):
        response = await client.post("/api/v1/billing/mpesa/callback", json=body)
        assert response.status_code == 200


async def test_another_students_reference_reveals_nothing(client, mpesa):
    """
    A reference is not a secret — it is on screens and in support threads. The
    status endpoint is scoped to the caller's own payments, so holding somebody
    else's buys neither information nor a plan.
    """
    mine, _ = await sign_in(client)
    theirs, _ = await sign_in(client, phone=OTHER_PHONE)

    reference = (await _pay(client, theirs)).json()["reference"]

    response = await client.get(
        f"/api/v1/billing/mpesa/status?reference={reference}", headers=mine
    )

    assert response.status_code == 404


async def test_a_stranger_cannot_credit_someone_elses_prompt(client, mpesa):
    """
    The callback credits the account named on the row, never one named in the
    request. There is no field in the callback body that could redirect it.
    """
    theirs, victim_id = await sign_in(client, phone=OTHER_PHONE)
    mine, attacker_id = await sign_in(client)

    reference = (await _pay(client, theirs)).json()["reference"]

    await client.post("/api/v1/billing/mpesa/callback", json=_callback())

    payment = await _payment(client, reference)
    assert payment.user_id == victim_id

    # The attacker got nothing.
    assert (
        await client.get("/api/v1/billing/subscription", headers=mine)
    ).json()["tier"] == "free"


# --- Falling back to Kora ------------------------------------------------------


async def test_an_unconfigured_daraja_falls_back_rather_than_failing(
    client, mpesa, monkeypatch, kora
):
    """
    A deployment with no Safaricom credentials must still be able to sell a
    plan. The student is not shown a provider outage; they get a payment page.
    """
    monkeypatch.setattr(settings, "daraja_consumer_key", "")

    headers, _ = await sign_in(client)
    body = (await _pay(client, headers)).json()

    assert body["mode"] == "redirect"
    assert body["provider"] == "kora"
    assert body["checkout_url"]


async def test_safaricom_refusing_the_push_falls_back(client, mpesa, kora):
    """
    Daraja answers 200 with a non-zero ResponseCode for a request it refused —
    a bad shortcode, a number it will not push to. Treating 200 as success is
    how a student is told to check a phone that never rings.
    """
    headers, _ = await sign_in(client)
    mpesa.push_response_code = "1"

    body = (await _pay(client, headers)).json()

    assert body["mode"] == "redirect"
    assert body["provider"] == "kora"


async def test_safaricom_being_unreachable_falls_back(client, mpesa, kora):
    headers, _ = await sign_in(client)
    mpesa.push_status = 503

    body = (await _pay(client, headers)).json()

    assert body["mode"] == "redirect"
    assert body["checkout_url"]


async def test_the_fallback_gets_its_own_reference(client, mpesa, kora):
    """
    Reusing the failed push's reference would put two providers' rows on one
    unique key and turn a fallback into a 500 at the worst possible moment.
    """
    headers, user_id = await sign_in(client)
    mpesa.push_response_code = "1"

    reference = (await _pay(client, headers)).json()["reference"]
    payment = await _payment(client, reference)

    assert payment.provider == "kora"
    assert payment.checkout_request_id is None


async def test_a_bad_number_is_refused_before_any_fallback(client, mpesa, kora):
    """
    A number that cannot receive an M-Pesa prompt cannot pay on either path, so
    sending the student to a payment page would just move the failure later.
    """
    headers, _ = await sign_in(client)

    response = await _pay(client, headers, phone="0812345678")

    assert response.status_code >= 400
    assert mpesa.push_payload is None


# --- Tokens -------------------------------------------------------------------


async def test_the_access_token_is_not_minted_per_payment(client, mpesa):
    """
    Daraja's OAuth endpoint is rate limited and its token lasts an hour. One per
    payment is a second request in front of every checkout, and a burst of them
    is how the endpoint starts refusing.
    """
    headers, _ = await sign_in(client)

    calls = []
    inner = mpesa.handle

    def counting(request):
        if "/oauth/" in str(request.url):
            calls.append(1)
        return inner(request)

    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(counting)
    )

    await _pay(client, headers)
    await _pay(client, headers)
    await _pay(client, headers)

    assert len(calls) == 1


# --- Support -------------------------------------------------------------------


async def test_the_console_reconciles_an_mpesa_payment_against_safaricom(
    client, mpesa
):
    """
    The reconcile button asks whoever actually processed the payment.

    Before the `provider` column it always asked Kora, so pressing it on an
    M-Pesa row got "that payment could not be verified" — which reads exactly
    like "the student never paid", on the one screen support uses to decide
    whether somebody was charged.
    """
    from tests.test_admin import admin_headers

    headers, _ = await sign_in(client)
    mpesa.still_processing = True
    reference = (await _pay(client, headers)).json()["reference"]

    # The callback never arrived and the student is complaining. Meanwhile the
    # payment really did go through.
    mpesa.still_processing = False

    admin = await admin_headers(client)
    response = await client.post(
        f"/api/v1/admin/payments/{reference}/reconcile", headers=admin
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    payment = await _payment(client, reference)
    assert payment.status == "success"

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "pro"


async def test_reconciling_a_live_prompt_does_not_close_it(client, mpesa):
    """
    A prompt thirty seconds old is still on somebody's handset. Marking it
    failed for being asked about too early loses a real payment.
    """
    from tests.test_admin import admin_headers

    headers, _ = await sign_in(client)
    mpesa.still_processing = True
    reference = (await _pay(client, headers)).json()["reference"]

    admin = await admin_headers(client)
    response = await client.post(
        f"/api/v1/admin/payments/{reference}/reconcile", headers=admin
    )

    assert response.status_code == 200
    assert "still being processed" in response.json()["message"]
    assert (await _payment(client, reference)).status == "pending"


async def test_verify_sends_an_mpesa_reference_to_the_right_endpoint(client, mpesa):
    """
    Kora has never heard of a Safaricom reference. Asked anyway it says "no such
    transaction", and `/billing/verify` would relay that as "your payment did
    not go through" — to somebody who has just been debited.
    """
    headers, _ = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    response = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )

    assert response.status_code == 409
    assert "mpesa/status" in response.json()["message"]


# --- Buy Goods ----------------------------------------------------------------
#
# The account this deployment actually uses. A till is not a paybill: two
# different numbers, in two fields, and the passkey belongs to one of them.


async def test_a_till_sends_the_store_number_and_the_till_separately(
    client, mpesa, monkeypatch
):
    """
    On Buy Goods, `BusinessShortCode` is the store (head office) number and
    `PartyB` is the till. They are different numbers and swapping them is
    rejected with an error naming a field rather than the mismatch.

    The passkey is issued against the *store* number, so that is also what signs
    the password — which is the part a paybill-shaped mental model gets wrong.
    """
    import base64

    monkeypatch.setattr(settings, "daraja_shortcode", "4001234")  # store / HO
    monkeypatch.setattr(settings, "daraja_party_b", "5551234")  # till
    monkeypatch.setattr(
        settings, "daraja_transaction_type", "CustomerBuyGoodsOnline"
    )
    daraja.reset_token_cache()

    headers, _ = await sign_in(client)
    await _pay(client, headers)

    payload = mpesa.push_payload

    assert payload["BusinessShortCode"] == "4001234"
    assert payload["PartyB"] == "5551234"
    assert payload["TransactionType"] == "CustomerBuyGoodsOnline"

    # Signed with the store number, not the till.
    expected = base64.b64encode(
        f"4001234passkey{payload['Timestamp']}".encode()
    ).decode()
    assert payload["Password"] == expected


async def test_the_status_query_uses_the_store_number_too(client, mpesa, monkeypatch):
    """
    The query authenticates the same way the push did. Sending the till here
    while the push sent the store number means every confirmation fails, and the
    symptom is payments that succeed and are never credited.
    """
    monkeypatch.setattr(settings, "daraja_shortcode", "4001234")
    monkeypatch.setattr(settings, "daraja_party_b", "5551234")
    monkeypatch.setattr(
        settings, "daraja_transaction_type", "CustomerBuyGoodsOnline"
    )
    daraja.reset_token_cache()

    headers, _ = await sign_in(client)
    reference = (await _pay(client, headers)).json()["reference"]

    await client.get(
        f"/api/v1/billing/mpesa/status?reference={reference}", headers=headers
    )

    assert mpesa.query_payload["BusinessShortCode"] == "4001234"


async def test_a_paybill_still_needs_only_one_number(client, mpesa, monkeypatch):
    """
    The fallback that keeps a paybill deployment from setting two fields that
    must agree — and then, one day, not agreeing.
    """
    monkeypatch.setattr(settings, "daraja_shortcode", "174379")
    monkeypatch.setattr(settings, "daraja_party_b", "")
    daraja.reset_token_cache()

    headers, _ = await sign_in(client)
    await _pay(client, headers)

    assert mpesa.push_payload["BusinessShortCode"] == "174379"
    assert mpesa.push_payload["PartyB"] == "174379"


# --- Flaky networks -----------------------------------------------------------
#
# Safaricom's resolver drops requests. The first real payment on this system
# fell back to Kora with "[Errno -3] Temporary failure in name resolution" —
# the fallback worked, and it also meant paying a processor's percentage for a
# blip that a retry would have ridden out.


def _both(mpesa, kora, fault=None):
    """
    Safaricom and Kora behind one transport, with an optional fault on the push.

    Composed rather than replacing the whole handler, because a test about
    Daraja failing is a test about *falling back* — and a transport that also
    breaks Kora is testing nothing but itself.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "korapay.com" in url:
            return kora.handle(request)
        if fault is not None and "/stkpush/v1/processrequest" in url:
            raised = fault()
            if raised is not None:
                raise raised
        return mpesa.handle(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_a_dns_blip_is_retried_rather_than_paid_around(client, mpesa, kora):
    """
    A connection that never opened is safe to retry: nothing reached Safaricom,
    so no prompt was sent and a second attempt cannot double-charge anyone.
    """
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.ConnectError(
                "[Errno -3] Temporary failure in name resolution"
            )
        return None

    outbound = _both(mpesa, kora, flaky)
    app.dependency_overrides[get_http_client] = lambda: outbound

    headers, _ = await sign_in(client)
    body = (await _pay(client, headers)).json()

    assert attempts["count"] == 2, "a connect failure should be retried once"
    assert body["mode"] == "stk", "the retry should have kept this on M-Pesa"
    assert body["provider"] == "daraja"


async def test_a_read_timeout_is_never_retried(client, mpesa, kora):
    """
    The one that must not be retried. A read timeout means the request *was*
    sent and the answer was lost, so Safaricom may already be ringing the
    student's phone — a second attempt puts two PIN prompts up for one plan.

    Falling back to a pricier provider is the cheap mistake here. Charging
    somebody twice is not.
    """
    attempts = {"count": 0}

    def timing_out():
        attempts["count"] += 1
        return httpx.ReadTimeout("timed out waiting for a response")

    outbound = _both(mpesa, kora, timing_out)
    app.dependency_overrides[get_http_client] = lambda: outbound

    headers, _ = await sign_in(client)
    body = (await _pay(client, headers)).json()

    assert attempts["count"] == 1, "a sent request must not be sent again"
    assert body["mode"] == "redirect", "and it should fall back instead"


async def test_a_persistent_outage_still_falls_back(client, mpesa, kora):
    """Retrying twice and giving up is the point; retrying for ever is not."""
    attempts = {"count": 0}

    def always_failing():
        attempts["count"] += 1
        return httpx.ConnectError("name resolution failed")

    outbound = _both(mpesa, kora, always_failing)
    app.dependency_overrides[get_http_client] = lambda: outbound

    headers, _ = await sign_in(client)
    body = (await _pay(client, headers)).json()

    assert attempts["count"] == 2, "exactly one retry, then give up"
    assert body["mode"] == "redirect"
    assert body["checkout_url"]


async def test_the_failure_log_names_the_host_and_environment(
    client, mpesa, kora, capsys
):
    """
    "Temporary failure in name resolution" says nothing about *which* name, and
    sandbox-versus-production is the first thing worth knowing when a payment
    cannot reach Safaricom. Both were missing from the log the first time this
    happened, which is why it took a guess to diagnose.
    """

    outbound = _both(
        mpesa,
        kora,
        lambda: httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
    )
    app.dependency_overrides[get_http_client] = lambda: outbound

    headers, _ = await sign_in(client)
    await _pay(client, headers)

    printed = capsys.readouterr().out
    assert "safaricom.co.ke" in printed
    assert "sandbox" in printed
