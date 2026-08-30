"""
Billing: the parts where being wrong costs money.

Webhook signatures, replay, seat accounting and what happens when a plan runs
out. Everything here is either a way to give the product away or a way to
charge someone twice.
"""

import hashlib
import hmac
import json
from datetime import datetime

import httpx
import pytest

from app.api.deps import get_http_client
from app.core.clock import now as utc_now
from app.core.config import settings
from app.main import app
from app.services.billing import activate, tier_from_charge
from app.services.kora import Charge, to_shillings, verify_signature
from app.services.plans import Tier, plan_for, saving_percent
from tests.conftest import OTHER_PHONE, sign_in

SECRET = "test-webhook-secret"


def _charge(amount_kes: int, *, tier: str | None = None, reference="ref_1") -> Charge:
    return Charge(
        reference=reference,
        status="success",
        amount_kes=amount_kes,
        channel="mobile_money",
        email="student@example.com",
        metadata={"tier": tier} if tier else {},
    )


def _signed(data: dict, *, event: str = "charge.success") -> tuple[bytes, str]:
    """
    A webhook as Kora sends one.

    The signature covers **only the ``data`` object**, serialised the way
    ``JSON.stringify`` would — no spaces. That is Kora's own convention, and it
    is the single easiest thing to get wrong when porting from a provider that
    signed the whole body.
    """
    # Built by concatenation rather than `json.dumps({...})` so the bytes on
    # the wire and the bytes that were signed cannot drift apart. Dumping the
    # whole body would put a space after every colon while the signed segment
    # has none — which is a bug in the fixture, not in the verifier, and it
    # takes a while to see that.
    segment = json.dumps(data, separators=(",", ":"))
    raw = f'{{"event":"{event}","data":{segment}}}'.encode()
    signature = hmac.new(SECRET.encode(), segment.encode(), hashlib.sha256).hexdigest()
    return raw, signature


# --- Signature ---------------------------------------------------------------


def test_a_forged_webhook_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "kora_webhook_secret", SECRET)
    raw, _ = _signed({"reference": "r"})

    assert verify_signature(raw, "not-the-signature") is False
    assert verify_signature(raw, None) is False


def test_a_genuine_webhook_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "kora_webhook_secret", SECRET)
    raw, signature = _signed({"reference": "r", "status": "success"})

    assert verify_signature(raw, signature) is True


def test_the_signature_covers_only_the_data_object(monkeypatch):
    """
    The Kora-specific rule, pinned.

    Signing the whole body is what a Paystack integration does, and it is
    exactly what this must not do — the digest would never match and every
    genuine delivery would be rejected as a forgery.
    """
    monkeypatch.setattr(settings, "kora_webhook_secret", SECRET)
    data = {"reference": "r", "status": "success"}
    raw, _ = _signed(data)

    whole_body = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert verify_signature(raw, whole_body) is False


def test_the_signature_survives_odd_number_formatting(monkeypatch):
    """
    Kora sends the amount as a decimal on some payloads.

    Parsing and re-serialising in Python turns ``350.00`` into ``350.0``, which
    changes the digest. The verifier slices the original bytes instead, so a
    body it never re-encodes still validates.
    """
    monkeypatch.setattr(settings, "kora_webhook_secret", SECRET)

    raw = b'{"event":"charge.success","data":{"amount":350.00,"reference":"r"}}'
    segment = b'{"amount":350.00,"reference":"r"}'
    signature = hmac.new(SECRET.encode(), segment, hashlib.sha256).hexdigest()

    assert verify_signature(raw, signature) is True


def test_whitespace_between_the_key_and_the_object_is_handled(monkeypatch):
    monkeypatch.setattr(settings, "kora_webhook_secret", SECRET)

    raw = b'{"event": "charge.success", "data" : {"reference":"r"}}'
    signature = hmac.new(
        SECRET.encode(), b'{"reference":"r"}', hashlib.sha256
    ).hexdigest()

    assert verify_signature(raw, signature) is True


def test_no_secret_means_nothing_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "kora_webhook_secret", "")
    monkeypatch.setattr(settings, "kora_secret_key", "")
    raw, signature = _signed({"reference": "r"})

    assert verify_signature(raw, signature) is False


def test_the_secret_key_signs_when_no_webhook_secret_is_set(monkeypatch):
    """Kora has no separate webhook secret — it signs with the API key."""
    monkeypatch.setattr(settings, "kora_webhook_secret", "")
    monkeypatch.setattr(settings, "kora_secret_key", SECRET)
    raw, signature = _signed({"reference": "r"})

    assert verify_signature(raw, signature) is True


# --- Amounts ------------------------------------------------------------------


def test_the_amount_is_read_as_whole_shillings():
    """
    The other trap. Kora charges the major unit, so 350 means KES 350 — there
    is no division by a hundred anywhere, and adding one back would credit a
    Synapse plan for three shillings.
    """
    assert to_shillings(350) == 350
    assert to_shillings("350.00") == 350
    assert to_shillings(1250.0) == 1250
    assert to_shillings(None) == 0
    assert to_shillings("not a number") == 0


# --- Which plan was paid for --------------------------------------------------


def test_metadata_alone_cannot_buy_a_plan():
    """
    Metadata is a field on a checkout we do not fully control. Trusting it
    would mean a KES 150 payment tagged `tier=pro` buys Synapse — the amount
    is the fact, and metadata only narrows it.
    """
    assert tier_from_charge(_charge(150, tier="pro")) is Tier.STANDARD
    assert tier_from_charge(_charge(350, tier="pro")) is Tier.PRO


def test_metadata_cannot_name_a_tier_that_is_not_for_sale():
    # "free" is a real Tier and worth nothing paid. It must never resolve.
    assert tier_from_charge(_charge(350, tier="free")) is Tier.PRO


def test_the_amount_resolves_the_tier_without_metadata():
    assert tier_from_charge(_charge(150)) is Tier.STANDARD
    assert tier_from_charge(_charge(350)) is Tier.PRO
    assert tier_from_charge(_charge(1250)) is Tier.FRIENDS


def test_paying_between_two_plans_gets_the_lower_one():
    """
    KES 1,000 is more than a Focus Season and less than a Synapse Season. It
    buys the Focus Season — resolving upward would hand out four months of
    Synapse for the price of four months of Focus.
    """
    assert tier_from_charge(_charge(1000)) is Tier.STANDARD_SEASON


def test_a_season_price_resolves_to_the_season():
    """
    The amounts have to stay distinguishable as plans are added. KES 1,250 is a
    Friends month rather than a Synapse Season, because it is worth more.
    """
    assert tier_from_charge(_charge(500)) is Tier.STANDARD_SEASON
    assert tier_from_charge(_charge(1100)) is Tier.PRO_SEASON
    assert tier_from_charge(_charge(1250)) is Tier.FRIENDS
    assert tier_from_charge(_charge(4200)) is Tier.FRIENDS_SEASON


def test_friends_is_cheaper_per_head_than_synapse_alone():
    """The entire proposition. If this ever inverts, the plan is pointless."""
    friends = plan_for(Tier.FRIENDS)
    assert friends.price_per_seat_ksh == 208
    assert friends.price_per_seat_ksh < plan_for(Tier.PRO).price_ksh


def test_an_amount_below_every_plan_is_refused():
    from app.core.errors import AppError

    with pytest.raises(AppError):
        tier_from_charge(_charge(20))


# --- Subscriptions ------------------------------------------------------------


async def test_renewing_extends_rather_than_restarts(client):
    """Paying a week early must not throw that week away."""
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        first = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        first_end = first.expires_at
        await session.commit()

    async with client.sessions() as session:
        second = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        await session.commit()

    days = plan_for(Tier.PRO).duration_days
    assert (second.expires_at - first_end).days == pytest.approx(days, abs=1)


async def test_a_season_runs_for_four_months(client):
    """
    What a Season buys is time. If this ever comes back thirty days, the plan
    has silently become an expensive Focus.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        subscription = await activate(
            session, user_id=user_id, tier=Tier.STANDARD_SEASON, verified=True
        )
        await session.commit()

    assert (subscription.expires_at - utc_now()).days == pytest.approx(120, abs=1)


async def test_a_season_buys_time_and_not_a_bigger_allowance(client):
    """
    Four months of Focus, not four months' questions in one lump.

    Worth asserting because the opposite is the intuitive reading of the price,
    and a student who believes it feels cheated in week three.
    """
    monthly = plan_for(Tier.STANDARD).limits
    season = plan_for(Tier.STANDARD_SEASON).limits

    assert season.monthly_ai_queries == monthly.monthly_ai_queries
    assert season is monthly  # The same object, so the two cannot drift.


def test_a_season_is_cheaper_per_month_than_paying_monthly():
    """The only reason to buy one."""
    for season, monthly in (
        (Tier.STANDARD_SEASON, Tier.STANDARD),
        (Tier.PRO_SEASON, Tier.PRO),
        (Tier.FRIENDS_SEASON, Tier.FRIENDS),
    ):
        assert (
            plan_for(season).price_per_month_ksh < plan_for(monthly).price_ksh
        ), season
        assert saving_percent(plan_for(season)) > 0
        # A monthly plan is the baseline, so it never wears a saving badge.
        assert saving_percent(plan_for(monthly)) == 0


async def test_the_plans_payload_pairs_each_card(client):
    """
    The toggle swaps a price in place, so the app has to know which two entries
    are one card. It pairs on `family` -- never by picking apart an id.
    """
    body = (await client.get("/api/v1/billing/plans")).json()

    by_family: dict[str, set[str]] = {}
    for plan in body:
        by_family.setdefault(plan["family"], set()).add(plan["billing_period"])

    assert by_family == {
        "focus": {"monthly", "season"},
        "synapse": {"monthly", "season"},
        "friends": {"monthly", "season"},
    }

    season = next(p for p in body if p["id"] == "pro_season")
    assert season["price_ksh"] == 1100
    # The line under the price, and the badge -- both computed here so they
    # cannot disagree with what is actually charged.
    assert season["price_per_month_ksh"] == 275
    assert season["saving_percent"] == 21
    assert season["duration_days"] == 120


async def test_a_friends_season_group_lasts_as_long_as_the_plan(client):
    """
    A Season's group living thirty days would end the plan three months early
    for everyone sitting in it.
    """
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(
            session, user_id=user_id, tier=Tier.FRIENDS_SEASON, verified=True
        )
        await session.commit()

    group = (await client.post("/api/v1/billing/group", headers=headers)).json()

    assert group["seats"] == 6
    expires = datetime.fromisoformat(group["expires_at"])
    assert (expires - utc_now()).days == pytest.approx(120, abs=1)


async def test_a_seat_on_a_season_reports_the_season(client):
    """A member's plan name has to be the one they are actually sitting on."""
    owner_headers, owner_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(
            session, user_id=owner_id, tier=Tier.FRIENDS_SEASON, verified=True
        )
        await session.commit()

    group = (
        await client.post("/api/v1/billing/group", headers=owner_headers)
    ).json()

    friend_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()

    assert subscription["tier"] == "friends_season"
    assert subscription["name"] == "Friends Season"


async def test_switching_tier_starts_a_fresh_period(client):
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=Tier.STANDARD, verified=True)
        switched = await activate(session, user_id=user_id, tier=Tier.PRO, verified=True)
        await session.commit()

    days = plan_for(Tier.PRO).duration_days
    assert (switched.expires_at - utc_now()).days == pytest.approx(days, abs=1)


async def test_subscription_endpoint_reports_the_free_plan(client):
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()
    assert body["tier"] == "free"
    # Free does not run out, so there is no countdown to report.
    assert body["expires_at"] is None
    # True, and it is what the paywall asks: there is no paid plan in force.
    assert body["is_expired"] is True


async def test_a_lapsed_plan_reports_as_expired(client):
    """
    Down to the free floor, not back to the trial — and not to a tier with
    nothing in it either. Someone who paid once and stopped is still a user.
    """
    _, user_id = await sign_in(client)

    async with client.sessions() as session:
        from sqlalchemy import select

        from app.models.billing import Subscription

        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription.tier = Tier.PRO.value
        subscription.expires_at = utc_now().replace(year=2020)
        await session.commit()

    headers, _ = await sign_in(client)
    body = (await client.get("/api/v1/billing/subscription", headers=headers)).json()
    assert body["tier"] == "free"
    assert body["is_expired"] is True
    # What they had is still reported, so the app can name what ended.
    assert body["nominal_tier"] == "pro"


async def test_only_sellable_plans_are_advertised(client):
    body = (await client.get("/api/v1/billing/plans")).json()

    ids = {plan["id"] for plan in body}
    assert ids == {
        "standard",
        "pro",
        "friends",
        "standard_season",
        "pro_season",
        "friends_season",
    }
    # Neither the free plan nor the legacy trial is a product.
    assert "trial" not in ids
    assert "free" not in ids

    friends = next(p for p in body if p["id"] == "friends")
    assert friends["seats"] == 6
    assert friends["price_per_seat_ksh"] == 208


# --- Checkout -----------------------------------------------------------------


class _FakeKora:
    """
    Stands in for Kora and remembers what it was sent.

    What matters in these tests is the *request* — the amount and the metadata
    are what a wrong checkout gets wrong, and both are decided on this side
    rather than by Kora.
    """

    def __init__(self):
        self.payload = None
        #: What a later verify should report. Tests that only exercise checkout
        #: never look at it.
        self.verify_status = "success"
        self.verify_amount = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Verifying a reference is a GET with no body; creating a charge is a
        # POST with one. Branching on the method keeps both in one fake.
        if request.method == "GET":
            reference = str(request.url).rstrip("/").rsplit("/", 1)[-1]
            amount = self.verify_amount
            if amount is None:
                amount = (self.payload or {}).get("amount", 0)
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Charge retrieved",
                    "data": {
                        "reference": reference,
                        "status": self.verify_status,
                        "amount": amount,
                        "currency": "KES",
                        "payment_method": "mobile_money",
                        "metadata": (self.payload or {}).get("metadata", {}),
                    },
                },
            )

        self.payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "Charge created successfully",
                "data": {
                    "checkout_url": "https://checkout.korapay.com/abc123/pay",
                    "reference": self.payload["reference"],
                },
            },
        )


@pytest.fixture
def kora(client, monkeypatch):
    """
    Swaps the *outbound* client only.

    Patching ``httpx.AsyncClient.post`` would also intercept the test client's
    own requests into the app, since both are httpx — so the seam is the
    dependency, not the library.
    """
    monkeypatch.setattr(settings, "kora_secret_key", "sk_test_x")
    fake = _FakeKora()

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    app.dependency_overrides[get_http_client] = lambda: outbound

    return fake


async def test_checkout_returns_a_link_and_its_reference(client, kora):
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/billing/checkout", json={"tier": "pro"}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["checkout_url"].startswith("https://")
    # Kept alongside `checkout_url` for one release, so a build already on a
    # student's phone does not lose its Upgrade button.
    assert body["authorization_url"] == body["checkout_url"]
    assert body["reference"]
    assert body["amount_ksh"] == plan_for(Tier.PRO).price_ksh


async def test_checkout_names_the_payer_in_the_metadata(client, kora):
    """
    The whole reason the link is issued here rather than shipped in the app.
    Without this, the charge that comes back belongs to nobody in particular
    and `assert_charge_belongs_to` has only an email to go on.
    """
    headers, user_id = await sign_in(client)

    await client.post(
        "/api/v1/billing/checkout", json={"tier": "friends"}, headers=headers
    )

    assert kora.payload["metadata"]["user_id"] == str(user_id)
    assert kora.payload["metadata"]["tier"] == "friends"
    # Kora caps metadata at five fields with names of at most twenty
    # characters, and rejects the nested array Paystack accepted.
    assert len(kora.payload["metadata"]) <= 5
    assert all(len(key) <= 20 for key in kora.payload["metadata"])
    assert all(isinstance(value, str) for value in kora.payload["metadata"].values())


async def test_checkout_prices_the_plan_from_the_server(client, kora):
    """
    In whole shillings, and from the plan table.

    Two claims in one test, both of which cost real money when wrong. The price
    comes from the server, because a price the client could send is a price the
    client could choose. And it goes out in the *major* unit — the previous
    provider took cents, and leaving that multiplier in place would bill
    KES 15,000 for a KES 150 plan.
    """
    headers, _ = await sign_in(client)

    await client.post(
        "/api/v1/billing/checkout", json={"tier": "standard"}, headers=headers
    )

    assert kora.payload["amount"] == plan_for(Tier.STANDARD).price_ksh
    assert kora.payload["amount"] == 150
    assert kora.payload["currency"] == "KES"


async def test_a_tier_that_is_not_for_sale_cannot_be_bought(client, kora):
    headers, _ = await sign_in(client)

    for tier in ("trial", "free", "nonsense"):
        response = await client.post(
            "/api/v1/billing/checkout", json={"tier": tier}, headers=headers
        )
        assert response.status_code == 400

    assert kora.payload is None


async def test_checkout_needs_a_signed_in_student(client, kora):
    response = await client.post("/api/v1/billing/checkout", json={"tier": "pro"})
    assert response.status_code == 401


async def test_a_student_with_no_email_can_still_pay(client, kora):
    """
    Phone sign-in collects no email and Kora demands one. Refusing to sell
    a plan over that would lock out most of the intended users.
    """
    headers, _ = await sign_in(client)

    await client.post(
        "/api/v1/billing/checkout", json={"tier": "pro"}, headers=headers
    )

    # Kora nests the payer under `customer`, where Paystack took a bare
    # `email` at the top level.
    assert "@" in kora.payload["customer"]["email"]


# --- Friends ------------------------------------------------------------------


async def test_a_group_needs_a_friends_plan_first(client):
    """Otherwise a group is five free Synapse seats."""
    headers, _ = await sign_in(client)

    assert (await client.post("/api/v1/billing/group", headers=headers)).status_code == 403


async def _friends_owner(client):
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=Tier.FRIENDS, verified=True)
        await session.commit()

    return headers, user_id


async def test_the_owner_holds_one_of_the_six_seats(client):
    headers, _ = await _friends_owner(client)

    group = (await client.post("/api/v1/billing/group", headers=headers)).json()
    assert group["seats"] == 6
    assert group["seats_taken"] == 1


async def test_a_friend_can_join_and_gets_the_entitlement(client):
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    joined = await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    assert joined.status_code == 200
    assert joined.json()["seats_taken"] == 2

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()
    assert subscription["tier"] == "friends"


async def test_joining_twice_is_not_an_error(client):
    """Tapping an invite link twice is not a mistake worth a message."""
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )
    again = await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    assert again.status_code == 200
    assert again.json()["seats_taken"] == 2


async def test_a_bad_invite_code_is_refused(client):
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/billing/group/join", json={"code": "NOPE1234"}, headers=headers
    )
    assert response.status_code == 404


async def test_the_owner_cannot_be_removed(client):
    """The seat would be unreclaimable and the group would outlive the payer."""
    headers, owner_id = await _friends_owner(client)
    await client.post("/api/v1/billing/group", headers=headers)

    response = await client.delete(
        f"/api/v1/billing/group/members/{owner_id}", headers=headers
    )
    assert response.status_code == 400


async def test_removing_a_member_frees_the_seat_and_the_entitlement(client):
    owner_headers, _ = await _friends_owner(client)
    group = (await client.post("/api/v1/billing/group", headers=owner_headers)).json()

    friend_headers, friend_id = await sign_in(client, phone=OTHER_PHONE)
    await client.post(
        "/api/v1/billing/group/join",
        json={"code": group["invite_code"]},
        headers=friend_headers,
    )

    removed = await client.delete(
        f"/api/v1/billing/group/members/{friend_id}", headers=owner_headers
    )
    assert removed.status_code == 204

    members = (
        await client.get("/api/v1/billing/group/members", headers=owner_headers)
    ).json()
    assert len(members) == 1

    # The entitlement came from the group and goes back with the seat — to
    # expired, never to a fresh trial.
    subscription = (
        await client.get("/api/v1/billing/subscription", headers=friend_headers)
    ).json()
    assert subscription["tier"] == "free"


async def test_someone_elses_group_is_not_yours_to_manage(client):
    owner_headers, _ = await _friends_owner(client)
    await client.post("/api/v1/billing/group", headers=owner_headers)

    stranger_headers, _ = await sign_in(client, phone=OTHER_PHONE)
    assert (
        await client.get("/api/v1/billing/group", headers=stranger_headers)
    ).status_code == 404


async def test_checkout_leaves_a_pending_payment_to_find(client, kora):
    """
    A charge that is started must be visible before it is confirmed.

    Without this row an unconfirmed payment left no trace at all: nothing in the
    console, and the admin reconcile endpoint useless because it looks payments
    up by reference. The first real payment on this system landed in exactly
    that gap — money taken, no record, nothing anyone could press.
    """
    from sqlalchemy import select

    from app.models.billing import Payment

    headers, user_id = await sign_in(client)

    started = await client.post(
        "/api/v1/billing/checkout", json={"tier": "pro"}, headers=headers
    )
    assert started.status_code == 200
    reference = started.json()["reference"]

    async with client.sessions() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.reference == reference)
        )

    assert payment is not None, "checkout recorded nothing to reconcile against"
    assert payment.status == "pending"
    assert payment.user_id == user_id
    assert payment.amount_kes == plan_for(Tier.PRO).price_ksh
    assert payment.paid_at is None


async def test_paying_for_a_friends_season_opens_the_group(client, kora):
    """
    The trailing step, on the tier most likely to be forgotten.

    Activation and group creation are two things, and every payment path has to
    do both. A Friends Season that activates without a group is six seats with
    no invite code to reach them by — the student paid KES 4,200 and has
    nothing to share.
    """
    headers, _ = await sign_in(client)

    started = await client.post(
        "/api/v1/billing/checkout", json={"tier": "friends_season"}, headers=headers
    )
    reference = started.json()["reference"]

    verified = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )
    assert verified.status_code == 200, verified.text

    group = await client.get("/api/v1/billing/group", headers=headers)
    assert group.status_code == 200, "the group was never created"
    assert group.json()["seats"] == 6


async def test_confirming_a_pending_payment_activates_the_plan(client, kora):
    """
    The pending row must not swallow the confirmation.

    `record_payment` returns an existing row untouched, which is what makes
    Kora's repeat deliveries safe. A pending row written at checkout would sit
    in that same path — so without the amendment, adding the row would have
    stopped every plan activating.
    """
    from sqlalchemy import select

    from app.models.billing import Payment

    headers, _ = await sign_in(client)

    started = await client.post(
        "/api/v1/billing/checkout", json={"tier": "pro"}, headers=headers
    )
    reference = started.json()["reference"]

    verified = await client.post(
        "/api/v1/billing/verify", json={"reference": reference}, headers=headers
    )
    assert verified.status_code == 200, verified.text

    subscription = (
        await client.get("/api/v1/billing/subscription", headers=headers)
    ).json()
    assert subscription["tier"] == "pro", "the plan never activated"

    async with client.sessions() as session:
        rows = (
            await session.scalars(
                select(Payment).where(Payment.reference == reference)
            )
        ).all()

    assert len(rows) == 1, "the pending row should be filled in, not duplicated"
    assert rows[0].status == "success"
    assert rows[0].paid_at is not None
