from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now as utc_now
from app.core.config import settings
from app.models.trial import TrialGrant
from app.services.plans import Tier, plan_for

log = structlog.get_logger()


@dataclass(frozen=True)
class TrialDecision:
    granted: bool
    reason: str = ""


def identity_hash(value: str, kind: str) -> str:
    """
    A stable, irreversible handle for a phone number or email.

    Keyed with the app secret so a leaked table cannot be brute-forced back
    into a list of phone numbers — the search space for Kenyan mobiles is
    about ten million, which a plain SHA-256 gives up in seconds.

    Normalised first, or ``+254712345678`` and ``+254 712 345 678`` are two
    identities and the whole defence is one space character wide.
    """
    normalised = value.strip().lower().replace(" ", "").replace("-", "")
    return hmac.new(
        settings.jwt_secret.encode(),
        f"{kind}:{normalised}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def has_had_trial(
    session: AsyncSession,
    *,
    phone: str | None = None,
    email: str | None = None,
    device_id: uuid.UUID | None = None,
) -> TrialGrant | None:
    """
    Finds any previous trial for this identity, however it signed up.

    Phone and email are checked together because one person is one person: a
    student who trialled on a number and then comes back through Google is not
    a new customer, and matching only the channel they used this time would
    hand them a second fortnight.
    """
    conditions = []

    if phone:
        conditions.append(TrialGrant.identity_hash == identity_hash(phone, "phone"))
    if email:
        conditions.append(TrialGrant.identity_hash == identity_hash(email, "email"))
    if device_id:
        conditions.append(TrialGrant.device_id == device_id)

    if not conditions:
        return None

    return await session.scalar(select(TrialGrant).where(or_(*conditions)).limit(1))


async def claim(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    phone: str | None = None,
    email: str | None = None,
    device_id: uuid.UUID | None = None,
) -> TrialDecision:
    """
    Grants the trial, once per identity, for good.

    Returns whether it was granted so the caller can decide what to do with a
    returning account — currently: create the subscription already expired, so
    they land on the paywall rather than on a broken screen.

    The row is written even when the trial is refused nothing new is recorded;
    the existing grant already says everything.
    """
    existing = await has_had_trial(
        session, phone=phone, email=email, device_id=device_id
    )

    if existing is not None:
        log.info(
            "trial_refused",
            user_id=str(user_id),
            reason="identity has already had a trial",
        )
        return TrialDecision(granted=False, reason="This number has already had a trial.")

    now = utc_now()
    days = plan_for(Tier.TRIAL).duration_days

    # One row per channel the account arrived with, so signing up by phone and
    # later linking Google cannot produce a second trial.
    for value, kind in ((phone, "phone"), (email, "email")):
        if not value:
            continue
        session.add(
            TrialGrant(
                identity_hash=identity_hash(value, kind),
                identity_kind=kind,
                granted_to_user_id=user_id,
                granted_at=now,
                expires_at=now + timedelta(days=days),
                device_id=device_id,
            )
        )

    await session.flush()
    log.info("trial_granted", user_id=str(user_id), days=days)
    return TrialDecision(granted=True)
