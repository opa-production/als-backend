import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UuidPK, UUIDPrimaryKey


class OtpCode(Base, UUIDPrimaryKey, Timestamps):
    """
    A one-time code, in the database rather than in memory.

    Memory is wrong here for two reasons. The service runs several workers, so
    a code minted on one would be unknown to the next — verification would fail
    roughly (N-1)/N of the time and look like a broken SMS provider. And a
    restart mid-signup would strand anyone holding a code.

    The code is stored **hashed**. A leaked table should not be a list of live
    codes for every phone number in the system.
    """

    __tablename__ = "otp_codes"

    #: E.164. Not a foreign key: the first code is sent before the account
    #: exists, which is exactly the point of it.
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: Counted so a code can be burned after a few wrong guesses. Six digits is
    #: a million possibilities; unlimited attempts makes that a formality.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Set on success, so the same code cannot be replayed.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # The verification lookup: newest unconsumed code for a number.
        Index("ix_otp_codes_phone_created", "phone", "created_at"),
        # The throttle sweep, and the cleanup job.
        Index("ix_otp_codes_expires", "expires_at"),
    )


class RefreshToken(Base, UUIDPrimaryKey, Timestamps):
    """
    A refresh token, hashed, tied to one device.

    Rotated on every use: presenting a refresh token returns a new one and
    revokes the old. If a stolen token is used, the real device's next refresh
    fails — which is a detectable signal rather than a silent shared session.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(UuidPK, index=True, nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidPK, index=True, nullable=True
    )

    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_refresh_tokens_user_revoked", "user_id", "revoked_at"),)
