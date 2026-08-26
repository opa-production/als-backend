from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import as_utc
from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.core.security import (
    admin_refresh_expiry,
    create_admin_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.admin import AdminRefreshToken, AdminUser

log = structlog.get_logger()

ROLES = ("support", "admin", "owner")

#: Long enough that a leaked list of admin emails is not a shortlist of
#: guessable logins. Not a policy engine — length is the only rule that
#: reliably matters, and the rest push people towards Passw0rd!.
MIN_PASSWORD_LENGTH = 12


def normalise_email(email: str) -> str:
    return email.strip().lower()


def assert_password_ok(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AppError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


async def create_admin(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str = "",
    role: str = "support",
) -> AdminUser:
    if role not in ROLES:
        raise AppError(f"Role must be one of: {', '.join(ROLES)}.")

    assert_password_ok(password)
    email = normalise_email(email)

    existing = await session.scalar(
        select(AdminUser).where(AdminUser.email == email)
    )
    if existing is not None:
        raise AppError("An admin with that email already exists.", status_code=409)

    admin = AdminUser(
        email=email,
        full_name=full_name,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(admin)
    await session.flush()
    return admin


async def authenticate(
    session: AsyncSession, *, email: str, password: str
) -> AdminUser:
    """
    Email and password to an admin row.

    The same message for an unknown email and a wrong password, and the hash is
    computed either way. Two different answers here turn the login form into an
    oracle for which addresses are admins, and an early return turns it into a
    timing one.
    """
    refused = AppError("Those details are not right.", status_code=401)

    admin = await session.scalar(
        select(AdminUser).where(AdminUser.email == normalise_email(email))
    )

    #: A hash of a value that cannot match, so a missing row costs the same
    #: ~100ms as a present one.
    stored = admin.password_hash if admin else hash_password("no-such-admin-here")

    if not verify_password(password, stored) or admin is None:
        raise refused

    if not admin.is_active:
        raise AppError("That admin account has been deactivated.", status_code=403)

    admin.last_login_at = utc_now()
    await session.flush()
    return admin


async def issue_session(
    session: AsyncSession,
    *,
    admin: AdminUser,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    """Mints an access token and a fresh, stored refresh token."""
    raw, digest = create_refresh_token()

    session.add(
        AdminRefreshToken(
            admin_id=admin.id,
            token_hash=digest,
            expires_at=admin_refresh_expiry(),
            ip=ip,
            user_agent=(user_agent or "")[:256] or None,
        )
    )
    await session.flush()

    return create_admin_token(admin.id), raw


async def rotate_session(
    session: AsyncSession, *, refresh_token: str, ip: str | None = None
) -> tuple[AdminUser, str, str]:
    """
    Exchanges a refresh token for a new pair, revoking the old one.

    Rotation on every use, as with the student tokens: if a stolen token is
    used, the real session's next refresh fails, which is a signal rather than
    a silently shared console.
    """
    refused = AppError("Please sign in again.", status_code=401)

    stored = await session.scalar(
        select(AdminRefreshToken).where(
            AdminRefreshToken.token_hash == hash_token(refresh_token)
        )
    )
    if stored is None or stored.revoked_at is not None:
        raise refused

    expires = as_utc(stored.expires_at)
    if expires is None or expires <= utc_now():
        raise refused

    admin = await session.get(AdminUser, stored.admin_id)
    if admin is None or not admin.is_active:
        raise refused

    stored.revoked_at = utc_now()
    access, raw = await issue_session(session, admin=admin, ip=ip)
    return admin, access, raw


async def revoke_session(session: AsyncSession, *, refresh_token: str) -> None:
    stored = await session.scalar(
        select(AdminRefreshToken).where(
            AdminRefreshToken.token_hash == hash_token(refresh_token)
        )
    )
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = utc_now()
        await session.flush()


async def revoke_all(session: AsyncSession, admin_id: uuid.UUID) -> int:
    """
    Ends every session an admin has.

    Called on deactivation and on a password change. Without it, deactivating
    a compromised account leaves its access token working for up to an hour —
    which is exactly the hour that matters.
    """
    rows = (
        await session.scalars(
            select(AdminRefreshToken).where(
                AdminRefreshToken.admin_id == admin_id,
                AdminRefreshToken.revoked_at.is_(None),
            )
        )
    ).all()

    now = utc_now()
    for row in rows:
        row.revoked_at = now

    await session.flush()
    return len(rows)


async def set_password(
    session: AsyncSession, *, admin: AdminUser, password: str
) -> None:
    assert_password_ok(password)
    admin.password_hash = hash_password(password)
    await session.flush()
    await revoke_all(session, admin.id)
