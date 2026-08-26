import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonB, Timestamps, UuidPK, UUIDPrimaryKey


class AdminUser(Base, UUIDPrimaryKey, Timestamps):
    """
    Whoever runs the business, which is not a student.

    A separate table rather than a flag on ``users``, for three reasons that
    each stand on their own:

    **The credential is different.** A student signs in with an SMS code to a
    Kenyan number. An admin signs in from a laptop with a password, and adding
    a password column to ``users`` would put a hashable secret on ten thousand
    rows that will never have one.

    **The token is different.** Admin tokens carry ``typ: admin`` and are
    rejected by the student endpoints, and student tokens are rejected here —
    see ``app/core/security.py``. With one table and a boolean, a stolen
    student token plus a flipped flag is total access.

    **Blast radius.** Nothing under ``/admin`` is scoped to one account. That
    is worth a login that cannot be obtained by holding someone's phone.
    """

    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(120), default="")

    #: scrypt, salted per row. See ``app/core/security.py``.
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)

    #: owner | admin | support. Checked per route, not per token — a demoted
    #: admin loses the power on their next request, not at their next login.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="support")

    #: Revocation without deletion, so the audit log keeps naming a real row.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AdminRefreshToken(Base, UUIDPrimaryKey, Timestamps):
    """
    An admin session, hashed and revocable.

    Same reasoning as the student one in ``app/models/auth.py``: a JWT cannot
    be taken back before it expires, and a console session is precisely the
    thing you want to be able to end from another window.
    """

    __tablename__ = "admin_refresh_tokens"

    admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Recorded for the session list, not for authorisation — an IP is trivial
    #: to spoof and useless as a check, but it is the first thing anyone wants
    #: to see when a login looks wrong.
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (
        Index("ix_admin_refresh_tokens_admin_revoked", "admin_id", "revoked_at"),
    )


class AdminAuditLog(Base, UUIDPrimaryKey, Timestamps):
    """
    Every admin action that changed something.

    Written in the same transaction as the change itself, so the log cannot
    disagree with the database — a log appended afterwards is a log that is
    missing exactly the entries that mattered, because those are the requests
    that crashed halfway.

    Reads are not logged. Logging them buries the twenty entries that matter
    under twenty thousand that do not, and the ones that matter are the ones
    that moved money or entitlement.
    """

    __tablename__ = "admin_audit_log"

    #: SET NULL rather than CASCADE: removing an admin must not erase what they
    #: did. ``admin_email`` below is the copy that survives it.
    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    #: Denormalised on purpose, for the line above.
    admin_email: Mapped[str] = mapped_column(String(320), default="")

    #: Dotted and past tense: ``subscription.granted``, ``user.deleted``.
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: What it was done to. ``user`` | ``subscription`` | ``payment`` | ``admin``.
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[uuid.UUID | None] = mapped_column(UuidPK, index=True, nullable=True)

    #: One line, already written for a human. The console shows this column and
    #: nothing else in the common case.
    summary: Mapped[str] = mapped_column(Text, default="")

    #: Before/after, or whatever the action needs to be reconstructible.
    meta: Mapped[dict | None] = mapped_column(JsonB, nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_admin_audit_log_created", "created_at"),)
