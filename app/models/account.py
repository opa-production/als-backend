import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDelete, Timestamps, UuidPK, UUIDPrimaryKey

if TYPE_CHECKING:
    # Import-time only. At runtime SQLAlchemy resolves these by name from its
    # registry, and importing them here for real would be a cycle.
    from app.models.billing import Subscription
    from app.models.course import Unit


class User(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """
    A student.

    Phone is the identity: sign-in is an SMS code, and a Kenyan student is far
    more likely to have a number than a university email they check. Email is
    optional and arrives with Google sign-in.
    """

    __tablename__ = "users"

    #: E.164, always. The app normalises before sending, and storing anything
    #: else makes "is this the same person" a string-formatting question.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)

    full_name: Mapped[str] = mapped_column(String(120), default="")
    institution: Mapped[str] = mapped_column(String(160), default="")
    program: Mapped[str] = mapped_column(String(160), default="")
    year_of_study: Mapped[int | None] = mapped_column(nullable=True)
    semester: Mapped[int | None] = mapped_column(nullable=True)

    #: Storage path in the `avatars` bucket, never a URL. URLs are signed and
    #: expire; the path is what stays true.
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Referrals --------------------------------------------------------
    #: This student's own code, minted the first time they look for it rather
    #: than at sign-up. Most accounts never open the referral screen, and a
    #: column filled in for all of them is a backfill and a uniqueness problem
    #: bought for nothing.
    referral_code: Mapped[str | None] = mapped_column(
        String(12), unique=True, index=True, nullable=True
    )
    #: Who brought them. Written once, at first sign-in, and never again —
    #: editable attribution is the most-used hole in every referral programme
    #: ever run ("let me add my friend's code now that I have paid").
    #:
    #: ``SET NULL`` rather than cascade: if the referrer deletes their account
    #: the person they brought is still a real student, and their own row must
    #: not go with it.
    referred_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    #: The one device allowed to be signed in.
    #:
    #: Checked on every request, not just at refresh. Relying on refresh alone
    #: leaves a thirty-minute window in which two phones both work — long
    #: enough to matter when the thing being shared is a paid account.
    active_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidPK, nullable=True
    )

    #: ``passive_deletes`` hands deletion to the database, whose ON DELETE
    #: rules already say what should happen. Without it SQLAlchemy tries to
    #: null the foreign keys itself first — which is a wasted UPDATE pass over
    #: every child row, and outright fails where the column is NOT NULL.
    units: Mapped[list["Unit"]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True
    )
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="user", lazy="raise", uselist=False, passive_deletes=True
    )

    __table_args__ = (
        # Sync pages on updated_at; without this it is a sequential scan per
        # device per open.
        Index("ix_users_updated_at", "updated_at"),
    )


class Device(Base, UUIDPrimaryKey, Timestamps):
    """
    One installation.

    Kept so a refresh token can be revoked for a lost phone without signing the
    student out everywhere, and so ``/sync`` can remember the high-water mark
    per device rather than per account — two devices are almost never at the
    same point.
    """

    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidPK, index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), default="")
    app_version: Mapped[str] = mapped_column(String(32), default="")

    #: Expo push token. Null until the student allows notifications.
    push_token: Mapped[str | None] = mapped_column(String(256), nullable=True)

    #: Hash, never the token. A leaked table must not be a set of live sessions.
    refresh_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "push_token", name="devices_user_push_token"),
    )
