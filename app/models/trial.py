from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UuidPK, UUIDPrimaryKey


class TrialGrant(Base, UUIDPrimaryKey, Timestamps):
    """
    A record that one identity has had its free trial. Ever.

    This table is the entire defence against the obvious abuse: use the
    fourteen days, delete the account, sign up on the same number, get another
    fourteen days, forever.

    Three properties make it work, and all three are deliberate:

    **It is not a foreign key to ``users``.** It has to outlive the account.
    Anything hanging off a user row disappears with the user row, which is
    exactly the moment this needs to still be true.

    **The identity is hashed, not stored.** A phone number in a table that is
    never deleted is a permanent record of everyone who ever tried the app.
    A keyed hash answers "has this number had a trial" without being a list of
    numbers.

    **Nothing deletes from here.** Not account deletion, not a support request.
    A row removed is a trial granted again.
    """

    __tablename__ = "trial_grants"

    #: HMAC of the normalised phone or email. See ``app/services/trial.py``.
    identity_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )

    #: "phone" or "email" — which channel this identity came from. Kept only so
    #: a support question can be answered without reversing anything.
    identity_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: The account that consumed it. Nullable because the account may be gone,
    #: and this row must survive that.
    granted_to_user_id: Mapped[str | None] = mapped_column(UuidPK, nullable=True)

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: The device that first claimed a trial on this identity.
    #:
    #: A second layer, and a weaker one — a reinstall mints a new device id, so
    #: this catches the careless case rather than the determined one. Worth
    #: having because the careless case is most of them.
    device_id: Mapped[str | None] = mapped_column(UuidPK, nullable=True)

    __table_args__ = (Index("ix_trial_grants_device", "device_id"),)
