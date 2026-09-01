"""
Cards, on a Paystack business shared with another product.

The sharing is the whole story here. Two dashboard settings belong to whoever
set the account up first, and each has a consequence this file pins:

* **The callback URL** is bypassed per transaction, so a student comes back to
  ALS while the other app's setting is untouched.
* **The webhook URL** cannot be bypassed, so no Paystack event ever reaches this
  service. Every card payment settles because *we asked* — on return, or from
  the sweep. If those two paths break, cards stop working entirely and nothing
  raises.

And running alongside another product means the account carries transactions
that are not ours. Everything under "Somebody else's money" is about not
crediting one.
"""

import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_http_client
from app.core.clock import now as utc_now
from app.core.config import settings
from app.main import app
from app.models.billing import Payment
from app.services import paystack, settlement
from app.services.kora import Charge
from app.services.plans import Tier, plan_for
from tests.conftest import OTHER_PHONE, sign_in

SECRET = "sk_test_paystack"


class FakePaystack:
    """
    Paystack, mocked at the transport so the request that is actually built —
    the amount, the currency, the per-transaction callback — is what gets
    asserted. Stubbing the adapter would test none of it.
    """

    def __init__(self):
        self.init_payload: dict | None = None
        self.status = "success"
        #: Minor units, as Paystack reports them. Left None to echo whatever
        #: was asked for.
        self.amount = None
        self.metadata: dict | None = None
        self.reference: str | None = None
        self.init_ok = True
        self.verify_status_code = 200
        self.verifies = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "/transaction/initialize" in url:
            self.init_payload = json.loads(request.content)
            if not self.init_ok:
                return httpx.Response(
                    200, json={"status": False, "message": "Currency not supported"}
                )
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {
                        "authorization_url": "https://checkout.paystack.com/abc123",
                        "access_code": "abc123",
                        "reference": self.init_payload["reference"],
                    },
                },
            )

        if "/transaction/verify/" in url:
            self.verifies += 1
            if self.verify_status_code != 200:
                return httpx.Response(
                    self.verify_status_code, json={"status": False}
                )
            reference = url.rstrip("/").rsplit("/", 1)[-1]
            amount = self.amount
            if amount is None:
                amount = (self.init_payload or {}).get("amount", 0)
            metadata = self.metadata
            if metadata is None:
                metadata = (self.init_payload or {}).get("metadata", {})
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": {
                        "reference": self.reference or reference,
                        "status": self.status,
                        "amount": amount,
                        "currency": "KES",
                        "channel": "card",
                        "customer": {"email": "student@example.com"},
                        "metadata": metadata,
                    },
                },
            )

        return httpx.Response(404)


@pytest.fixture
def cards(client, monkeypatch):
    monkeypatch.setattr(settings, "paystack_secret_key", SECRET)
    monkeypatch.setattr(
        settings, "paystack_callback_override", "https://als.test/card/return"
    )
    fake = FakePaystack()
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    app.dependency_overrides[get_http_client] = lambda: outbound
    return fake


async def _buy(client, headers, tier="pro"):
    return await client.post(
        "/api/v1/billing/card", json={"tier": tier}, headers=headers
    )


async def _payment(client, reference) -> Payment:
    async with client.sessions() as session:
        return await session.scalar(
            select(Payment).where(Payment.reference == reference)
        )


# --- Amounts ------------------------------------------------------------------


def test_shillings_go_out_as_minor_units():
    """
    Paystack prices in the minor unit and Kora in the major one. A Kora adapter
    copied across undercharges by a hundred — KES 3.50 for a KES 350 plan — and
    nothing about the response says so.
    """
    assert paystack.MINOR_UNITS == 100
    assert paystack.to_shillings(35000) == 350
    assert paystack.to_shillings("35000") == 350
    assert paystack.to_shillings(None) == 0
    assert paystack.to_shillings("nonsense") == 0


async def test_the_charge_is_the_plan_price_in_cents(client, cards):
    headers, _ = await sign_in(client)

    await _buy(client, headers)

    assert cards.init_payload["amount"] == plan_for(Tier.PRO).price_ksh * 100
    assert cards.init_payload["currency"] == "KES"


async def test_the_price_cannot_be_chosen_by_the_client(client, cards):
    headers, _ = await sign_in(client)

    await client.post(
        "/api/v1/billing/card",
        json={"tier": "standard", "amount": 1, "amount_ksh": 1, "price_ksh": 1},
        headers=headers,
    )

    assert cards.init_payload["amount"] == plan_for(Tier.STANDARD).price_ksh * 100


# --- Sharing the account ------------------------------------------------------


async def test_the_callback_is_sent_per_transaction(client, cards):
    """
    The dashboard's callback URL belongs to the other app. `callback_url` on
    `transaction/initialize` overrides it for this transaction only, which is
    what makes a borrowed account workable without touching their settings.
    """
    headers, _ = await sign_in(client)

    await _buy(client, headers)

    assert cards.init_payload["callback_url"] == "https://als.test/card/return"


async def test_only_the_card_channel_is_offered(client, cards):
    """
    Mobile money here would be the same M-Pesa payment Daraja takes directly,
    with a processor's cut on top — and offering it would quietly route students
    off the cheap path.
    """
    headers, _ = await sign_in(client)

    await _buy(client, headers)

    assert cards.init_payload["channels"] == ["card"]


async def test_our_transactions_are_identifiable_on_a_shared_dashboard(client, cards):
    headers, user_id = await sign_in(client)

    reference = (await _buy(client, headers)).json()["reference"]

    assert reference.startswith(paystack.REFERENCE_PREFIX)
    assert cards.init_payload["metadata"]["user_id"] == str(user_id)


# --- Somebody else's money ----------------------------------------------------


def _foreign(reference="other_app_txn_9912", user_id=None) -> Charge:
    return Charge(
        reference=reference,
        status="success",
        amount_kes=350,
        channel="card",
        email="someone@elsewhere.test",
        metadata={"user_id": user_id} if user_id else {},
    )


def test_a_transaction_from_the_other_app_is_not_ours():
    """
    The rule that makes a shared account safe.

    Every event on the business concerns transactions from both products.
    Crediting on amount alone would mean somebody's KES 350 purchase in a
    completely different app turning on a plan here.
    """
    assert paystack.is_ours(_foreign()) is False
    # Our prefix but no user: an old reference, or a guess.
    assert paystack.is_ours(_foreign(reference="als_abc")) is False
    # A user id but not our reference: not something we opened.
    assert paystack.is_ours(_foreign(user_id="4f1b")) is False
    # Both, which only this service sets.
    assert paystack.is_ours(_foreign(reference="als_abc", user_id="4f1b")) is True


async def test_verifying_a_foreign_reference_buys_nothing(client, cards):
    """
    A student pastes a reference from the other app — off a receipt, a
    screenshot, a support thread. It verifies perfectly well at Paystack. It
    must still buy nothing here.
    """
    headers, _ = await sign_in(client)
    cards.reference = "other_app_txn_9912"
    cards.metadata = {}

    async with client.sessions() as session:
        session.add(
            Payment(
                user_id=(await sign_in(client, phone=OTHER_PHONE))[1],
                reference="other_app_txn_9912",
                provider="paystack",
                tier="pro",
                amount_kes=350,
                status="pending",
            )
        )
        await session.commit()

    response = await client.post(
        "/api/v1/billing/verify",
        json={"reference": "other_app_txn_9912"},
        headers=headers,
    )

    assert response.status_code in (403, 409)


# --- Settling without a webhook -----------------------------------------------


async def test_a_card_payment_is_settled_by_asking(client, cards):
    """
    The primary path, because there is no other one: Paystack's webhook goes to
    the dashboard URL, which on this shared account is the other app's.
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]

    response = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["tier"] == "pro"
    assert cards.verifies >= 1


async def test_verify_asks_paystack_not_kora(client, cards):
    """
    Providers do not know each other's references. Asked of the wrong one, the
    answer is "no such transaction" — which this endpoint would relay to a
    student who has just been charged as "that payment has not gone through".
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]

    await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )

    payment = await _payment(client, reference)
    assert payment.provider == "paystack"
    assert payment.status == "success"


async def test_verifying_twice_does_not_extend_the_plan_twice(client, cards):
    headers, user_id = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]

    first = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )
    second = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )

    assert first.json()["expires_at"] == second.json()["expires_at"]


async def test_an_unpaid_checkout_is_refused(client, cards):
    headers, _ = await sign_in(client)
    cards.status = "abandoned"
    reference = (await _buy(client, headers)).json()["reference"]

    response = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )

    assert response.status_code == 402


async def test_the_return_page_credits_nothing(client, cards):
    """
    Paystack redirects a browser here with no token. Crediting from an
    unauthenticated GET carrying a reference would be a plan for anyone who can
    read a URL out of somebody's history.
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]

    response = await client.get(f"/api/v1/billing/card/return?reference={reference}")

    assert response.status_code == 200
    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "free", "a browser redirect must not buy a plan"


# --- The sweep ----------------------------------------------------------------
#
# The only route to a subscription for a student who paid and then closed the
# tab. With no webhook, nothing else would ever ask.


async def _age(client, reference, minutes):
    from datetime import timedelta

    async with client.sessions() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.reference == reference)
        )
        payment.created_at = utc_now() - timedelta(minutes=minutes)
        await session.commit()


async def test_the_sweep_rescues_a_student_who_closed_the_tab(client, cards):
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]
    await _age(client, reference, 10)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        assert await settlement.sweep(session, client=outbound) == 1

    assert (await _payment(client, reference)).status == "success"
    assert (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()["tier"] == "pro"


async def test_the_sweep_leaves_a_checkout_that_just_started_alone(client, cards):
    """
    A card checkout ninety seconds old is a student still typing their number.
    Closing it out as failed throws away a real payment.
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        assert await settlement.sweep(session, client=outbound) == 0

    assert (await _payment(client, reference)).status == "pending"
    assert cards.verifies == 0


async def test_the_sweep_gives_up_on_ancient_rows(client, cards):
    """
    Anything still pending after a day was abandoned. Chasing it for ever turns
    the sweep into a growing query against the whole payments table.
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]
    await _age(client, reference, 60 * 48)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        assert await settlement.sweep(session, client=outbound) == 0

    assert cards.verifies == 0


async def test_the_sweep_does_not_credit_an_abandoned_checkout(client, cards):
    headers, _ = await sign_in(client)
    cards.status = "abandoned"
    reference = (await _buy(client, headers)).json()["reference"]
    await _age(client, reference, 10)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        assert await settlement.sweep(session, client=outbound) == 0

    assert (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()["tier"] == "free"


async def test_a_processor_outage_leaves_the_payment_for_next_time(client, cards):
    """
    Paystack having a bad minute is not a student who did not pay. The row stays
    pending rather than being marked failed, because failed is terminal.
    """
    headers, _ = await sign_in(client)
    reference = (await _buy(client, headers)).json()["reference"]
    await _age(client, reference, 10)
    cards.verify_status_code = 502

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        assert await settlement.sweep(session, client=outbound) == 0

    assert (await _payment(client, reference)).status == "pending"


async def test_one_bad_payment_does_not_end_the_pass(client, cards):
    """
    The next nineteen rows may each be somebody locked out of a plan they paid
    for, so a single unhappy one is logged and stepped over.
    """
    headers, _ = await sign_in(client)
    good = (await _buy(client, headers)).json()["reference"]
    await _age(client, good, 10)

    async with client.sessions() as session:
        session.add(
            Payment(
                user_id=(await sign_in(client, phone=OTHER_PHONE))[1],
                reference="als_broken_row",
                provider="paystack",
                # A tier that no longer exists resolves to nothing and raises.
                tier="not_a_tier_any_more",
                amount_kes=350,
                status="pending",
                created_at=utc_now(),
            )
        )
        await session.commit()
    await _age(client, "als_broken_row", 10)

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(cards.handle))
    async with client.sessions() as session:
        activated = await settlement.sweep(session, client=outbound)

    assert activated >= 1, "the good payment must still be settled"


# --- Signatures ---------------------------------------------------------------
#
# Unreachable while the webhook points at the other app. Kept correct because it
# is the piece that would be written wrong in a hurry the day this moves to its
# own account.


def test_the_signature_covers_the_whole_body_with_sha512(monkeypatch):
    monkeypatch.setattr(settings, "paystack_secret_key", SECRET)
    raw = b'{"event":"charge.success","data":{"reference":"als_x"}}'

    good = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
    assert paystack.verify_signature(raw, good) is True

    # Kora's convention: SHA-256 over the `data` object only. Neither digest
    # validates the other, and transplanting one is silent.
    kora_style = hmac.new(
        SECRET.encode(), b'{"reference":"als_x"}', hashlib.sha256
    ).hexdigest()
    assert paystack.verify_signature(raw, kora_style) is False
    assert paystack.verify_signature(raw, None) is False
    assert paystack.verify_signature(raw, "nonsense") is False
