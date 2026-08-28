from __future__ import annotations

import re
import secrets
import uuid
from datetime import timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    create_otp,
    create_refresh_token,
    hash_token,
    refresh_expiry,
    verify_otp,
)
from app.models.account import Device, User
from app.models.auth import OtpCode, RefreshToken
from app.models.billing import Subscription
from app.services import trial as trial_service
from app.services.plans import Tier, plan_for

log = structlog.get_logger()

#: E.164: a plus, a country code that cannot start with zero, then digits.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")

def normalise_phone(phone: str) -> str:
    """
    One canonical form, or a refusal.

    The app already sends E.164, but this is the boundary and a boundary that
    trusts its caller is not one. Two spellings of the same number would become
    two accounts, which is unrecoverable without a merge tool nobody has built.
    """
    cleaned = re.sub(r"[\s\-()]", "", phone or "")
    if not E164.match(cleaned):
        raise AppError("That does not look like a valid phone number.")
    return cleaned


# --- The store review account ------------------------------------------------


def is_review_phone(phone: str) -> bool:
    """
    Whether this is the number given to Google Play and App Store reviewers.

    A reviewer cannot receive our SMS, so one declared number takes a fixed
    code instead — see ``review_phone`` in app/core/config.py. Comparison is on
    the normalised form so a reviewer typing the number with spaces still gets
    in.
    """
    if not settings.review_account_configured:
        return False
    try:
        return normalise_phone(phone) == normalise_phone(settings.review_phone)
    except AppError:
        # A malformed REVIEW_PHONE disables the account rather than matching
        # everything that fails to normalise alongside it.
        return False


async def _grant_review_entitlement(session: AsyncSession, *, user: User) -> None:
    """
    Keeps the review account on a full plan that does not lapse.

    The trial it was born with runs out after a fortnight, and an app store
    review can come months after the account was made. A reviewer who signs in
    to a paywall reports the app as broken, so this account is topped back up
    to Synapse on every sign-in.

    Written on sign-in rather than once at creation because the point is that
    it is *never* expired when someone actually looks at it.
    """
    now = utc_now()
    subscription = await session.scalar(
        select(Subscription).where(Subscription.user_id == user.id)
    )

    if subscription is None:
        subscription = Subscription(user_id=user.id, started_at=now)
        session.add(subscription)

    subscription.tier = Tier.PRO.value
    subscription.expires_at = now + timedelta(days=365)
    # Nothing was paid, and nothing is waiting on Kora to confirm it — an
    # unverified paid tier reads as expired in `get_entitlement`.
    subscription.verified = True

    await session.flush()


# --- One-time codes ----------------------------------------------------------


async def request_otp(session: AsyncSession, *, phone: str) -> str | None:
    """
    Mints a code and returns it, for the caller to send.

    Returning it rather than sending it here keeps this function free of the
    SMS provider, which is what lets the whole flow be tested without one.

    Returns ``None`` for the store review number: its code is fixed and already
    written in the review notes, so there is nothing to mint and nothing to
    send. The caller must treat that as success — a reviewer, and anyone else
    poking at the endpoint, sees exactly the response every other number gets.

    Throttled per number. An unthrottled send endpoint is a bill anyone can run
    up, and a way to use this service to text strangers.
    """
    phone = normalise_phone(phone)

    if is_review_phone(phone):
        log.info("otp_skipped_review_account", phone=phone)
        return None

    now = utc_now()

    recent = await session.scalar(
        select(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.created_at > now - timedelta(hours=1))
        .order_by(OtpCode.created_at.desc())
        .limit(settings.otp_max_sends_per_hour)
        .offset(settings.otp_max_sends_per_hour - 1)
    )
    if recent is not None:
        raise AppError(
            "Too many codes requested. Try again in a little while.", status_code=429
        )

    # Any outstanding code for this number stops working the moment a new one
    # is sent, so two codes are never live at once.
    await session.execute(
        update(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )

    code, code_hash = create_otp()
    session.add(
        OtpCode(
            phone=phone,
            code_hash=code_hash,
            expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
        )
    )
    await session.flush()

    return code


async def verify_otp_code(
    session: AsyncSession,
    *,
    phone: str,
    code: str,
    device_id: uuid.UUID | None = None,
) -> User:
    """
    Checks a code and returns the account, creating it on first use.

    Every failure below says the same thing. Distinguishing "no code for this
    number" from "wrong code" turns this endpoint into a way to find out which
    numbers have accounts.
    """
    phone = normalise_phone(phone)
    now = utc_now()
    generic = AppError("That code is not right, or it has expired.", status_code=401)

    if is_review_phone(phone):
        # The fixed code, compared without an early exit so the comparison
        # itself gives nothing away. No OtpCode row is involved: none was
        # written, and none should be, or a reviewer signing in would consume
        # a code and the next attempt would fail.
        if not secrets.compare_digest(code.strip(), settings.review_otp_code):
            raise generic

        user = await session.scalar(select(User).where(User.phone == phone))
        if user is None:
            user = await create_user(session, phone=phone, device_id=device_id)
        else:
            _reactivate(user)

        await _grant_review_entitlement(session, user=user)
        log.info("review_account_signed_in", user_id=str(user.id))
        return user

    record = await session.scalar(
        select(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.consumed_at.is_(None))
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )

    if record is None or as_utc(record.expires_at) <= now:
        raise generic

    if record.attempts >= settings.otp_max_attempts:
        # Burn it rather than leaving a code with unlimited guesses attached.
        record.consumed_at = now
        raise generic

    if not verify_otp(code, record.code_hash):
        record.attempts += 1
        raise generic

    record.consumed_at = now

    user = await session.scalar(select(User).where(User.phone == phone))

    if user is None:
        return await create_user(session, phone=phone, device_id=device_id)

    return _reactivate(user)


# --- Accounts ----------------------------------------------------------------


async def find_user_by_phone(session: AsyncSession, phone: str) -> User | None:
    """
    Looks up an account without creating one.

    Called before verification so the response can say whether this request is
    what created the account — the app sends a new student to onboarding and a
    returning one straight to the tabs.
    """
    try:
        normalised = normalise_phone(phone)
    except AppError:
        return None
    return await session.scalar(select(User).where(User.phone == normalised))


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email.strip().lower()))


async def create_user(
    session: AsyncSession,
    *,
    phone: str | None = None,
    email: str | None = None,
    full_name: str = "",
    device_id: uuid.UUID | None = None,
) -> User:
    """
    A new account, with a trial only if this identity has never had one.

    One trial per person, for good — see ``app/services/trial.py``. A returning
    account gets a subscription that is **already expired** rather than none at
    all: the row keeps every downstream query simple, and the student lands on
    the paywall instead of on a screen that cannot decide what they are.
    """
    user = User(phone=phone, email=email, full_name=full_name)
    session.add(user)
    await session.flush()

    decision = await trial_service.claim(
        session, user_id=user.id, phone=phone, email=email, device_id=device_id
    )

    now = utc_now()
    days = plan_for(Tier.TRIAL).duration_days

    session.add(
        Subscription(
            user_id=user.id,
            tier=Tier.TRIAL.value,
            started_at=now,
            # Already over when the trial was refused. `get_entitlement` reads
            # that as EXPIRED, so the restriction is immediate and needs no
            # separate flag to remember it.
            expires_at=now + timedelta(days=days) if decision.granted else now,
            # A trial is not a payment, so there is nothing to reconcile.
            verified=True,
        )
    )
    await session.flush()

    log.info(
        "user_created",
        user_id=str(user.id),
        via="phone" if phone else "google",
        trial_granted=decision.granted,
    )
    return user


def _reactivate(user: User) -> User:
    """
    Brings back an account inside its deletion window.

    Without this a deleted number is simply locked out forever: the row still
    holds the unique phone, so a new account cannot be created, and the old one
    is refused for being deleted. The student is left with a number that can
    never sign in again and no way to find out why.

    Deletion is a tombstone with a retention window — reactivating inside it is
    what the window is *for*. Nothing about the trial changes: the subscription
    comes back exactly as expired as it was, and the grant that says this
    identity has had its fortnight is untouched.
    """
    if user.deleted_at is not None:
        user.deleted_at = None
        log.info("account_reactivated", user_id=str(user.id))

    return user


async def upsert_google_user(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    device_id: uuid.UUID | None = None,
) -> User:
    """
    Finds or creates the account behind a verified Google address.

    Matching on email links a Google sign-in to an account that started with a
    phone number, which is what stops one student ending up with two.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        # A name is only filled in if we do not already have one — a student
        # who set their own should not have it overwritten by Google's.
        if not user.full_name and full_name:
            user.full_name = full_name
        return _reactivate(user)

    return await create_user(
        session, email=email, full_name=full_name, device_id=device_id
    )


# --- Sessions ----------------------------------------------------------------


async def issue_tokens(
    session: AsyncSession, *, user: User, device_id: uuid.UUID | None = None
) -> tuple[str, str]:
    """
    Access token plus a fresh refresh token, the plain value returned once.

    Signing in **takes over** the account: every other device is revoked and
    this one becomes the active session. One account, one phone.

    That is a deliberate product decision, not a technical one — a plan bought
    for one student otherwise covers a whole hostel. The person being signed
    out is the one who owns the number, and they signed in to cause it, so it
    needs no warning.
    """
    if device_id is not None and user.active_device_id != device_id:
        await revoke_device_tokens(session, user_id=user.id, device_id=None)
        user.active_device_id = device_id
        log.info(
            "session_taken_over", user_id=str(user.id), device_id=str(device_id)
        )

    raw, token_hash = create_refresh_token()

    session.add(
        RefreshToken(
            user_id=user.id,
            device_id=device_id,
            token_hash=token_hash,
            expires_at=refresh_expiry(),
        )
    )
    await session.flush()

    return create_access_token(user.id, device_id=device_id), raw


async def rotate_refresh_token(
    session: AsyncSession, *, raw_token: str
) -> tuple[User, str, str]:
    """
    Exchanges a refresh token for a new pair, revoking the old one.

    Rotation is what turns a stolen token into a detectable event: the thief
    and the real device cannot both keep refreshing, so one of them starts
    failing instead of quietly sharing the session forever.
    """
    now = utc_now()
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token))
    )

    if record is None or record.revoked_at is not None or as_utc(record.expires_at) <= now:
        raise AppError("Please sign in again.", status_code=401)

    record.revoked_at = now

    user = await session.get(User, record.user_id)
    if user is None or user.deleted_at is not None:
        raise AppError("Please sign in again.", status_code=401)

    access, refresh = await issue_tokens(session, user=user, device_id=record.device_id)
    return user, access, refresh


async def revoke_device_tokens(
    session: AsyncSession, *, user_id: uuid.UUID, device_id: uuid.UUID | None
) -> None:
    """Signs out one device, or all of them when no device is named."""
    statement = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    if device_id is not None:
        statement = statement.where(RefreshToken.device_id == device_id)

    await session.execute(statement)


async def register_device(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    device_id: uuid.UUID | None,
    platform: str,
    app_version: str,
) -> Device:
    """
    Records the installation, keyed on an id the client chose.

    Same rule as everywhere else: the device mints its own id, so re-registering
    after a reinstall updates one row instead of accumulating a row per launch.
    """
    device = await session.get(Device, device_id) if device_id else None

    if device is None:
        device = Device(
            id=device_id or uuid.uuid4(),
            user_id=user_id,
            platform=platform,
            app_version=app_version,
        )
        session.add(device)
    else:
        device.user_id = user_id
        device.platform = platform or device.platform
        device.app_version = app_version or device.app_version

    await session.flush()
    return device
