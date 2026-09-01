import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.account import User


class Subscription(Base, UUIDPrimaryKey, Timestamps):
    """
    What a student is entitled to, decided here rather than on the phone.

    The device keeps a copy so the app works offline, but this row is the
    authority. Anything else means the entitlement is whatever the client says
    it is, which is not an entitlement.
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    #: trial | standard | pro | friends — the ids in the app's plans.js.
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="trial")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: False until a Kora webhook has confirmed the money. The app already
    #: writes unverified subscriptions when a student says they paid, so this
    #: is the column that reconciles those against reality.
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Set when the seat came from someone else's Friends plan.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_groups.id", ondelete="SET NULL"), index=True, nullable=True
    )

    user: Mapped["User"] = relationship(back_populates="subscription", lazy="raise")

    __table_args__ = (Index("ix_subscriptions_expires", "expires_at"),)


class PlanGroup(Base, UUIDPrimaryKey, Timestamps):
    """
    A Friends plan: one payment, five seats.

    Kept separate from Subscription because the payer's entitlement and the
    group's capacity are different facts. Collapsing them means you cannot
    answer "who is still on this plan" after the payer leaves.
    """

    __tablename__ = "plan_groups"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="friends")
    seats: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    #: Short, shareable, and rotatable. Rotating invalidates outstanding
    #: invites without disturbing anyone already on the plan.
    invite_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    members: Mapped[list["PlanGroupMember"]] = relationship(
        back_populates="group",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PlanGroupMember(Base, UUIDPrimaryKey, Timestamps):
    """One seat. The owner holds one of them."""

    __tablename__ = "plan_group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_groups.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    group: Mapped["PlanGroup"] = relationship(back_populates="members", lazy="raise")

    __table_args__ = (
        # One seat each. Without this a shared invite link lets one person take
        # the whole plan by tapping it five times.
        UniqueConstraint("group_id", "user_id", name="plan_group_members_group_user"),
    )


class Payment(Base, UUIDPrimaryKey, Timestamps):
    """
    One transaction, whoever processed it.

    Nothing here is a card number. The reference and the status are all a
    webhook gives us and all we have any business keeping.
    """

    __tablename__ = "payments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: Our own reference, minted at checkout and unique, so a webhook delivered
    #: three times — which it will be — credits the plan exactly once.
    reference: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    #: Which provider is the authority on this row: `daraja`, `kora` or
    #: `paystack`.
    #:
    #: Not decoration. `/billing/verify` and the console's reconcile button both
    #: have to ask *somebody* what happened to a reference, and asking the wrong
    #: provider returns "no such transaction" — which reads identically to "they
    #: did not pay". Existing rows predate the column and are all Kora, which is
    #: what the migration backfills.
    provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="kora", server_default="kora"
    )

    #: Safaricom's handle for one STK prompt, and the only thing that ties an
    #: unauthenticated M-Pesa callback back to a row this service created.
    #:
    #: Indexed because the callback arrives knowing nothing else — no user, no
    #: reference of ours it can be trusted on, just this — so it is the lookup
    #: key on a path that must not table-scan. Null for every other provider.
    checkout_request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    #: The code on the student's M-Pesa SMS. Kept because it is what somebody
    #: quotes to support, never used to decide anything.
    receipt: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_kes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: pending | success | failed | abandoned
    status: Mapped[str] = mapped_column(String(16), default="pending")
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_payments_user_created", "user_id", "created_at"),)


class UsageCounter(Base, UUIDPrimaryKey, Timestamps):
    """
    Metered usage, one row per user per period.

    A row per *event* would be the obvious design and the wrong one: a hundred
    thousand rows a day to answer "how many questions today". Counters are
    incremented in place and the period key is what makes them roll over.

    ``period_key`` is the local day, ISO week or month depending on ``metric``,
    matching the intervals in the app's plan config exactly.
    """

    __tablename__ = "usage_counters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: ai_queries | quizzes | ocr_pages | pdf_pages
    metric: Mapped[str] = mapped_column(String(24), nullable=False)
    #: "2026-08" | "lifetime". Day and ISO-week keys were written here until
    #: allowances moved onto a monthly clock; rows carrying them are simply
    #: never read again, which is why nothing had to be migrated.
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The local day the row was opened on. Kept for cheap "what did they spend
    #: this week" reads in the console, which the period key can no longer
    #: answer now that it names a month.
    period_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        # The upsert target: INSERT ... ON CONFLICT DO UPDATE SET count = count + 1
        # is one round trip and is safe under concurrency, which a read-then-write
        # would not be.
        UniqueConstraint(
            "user_id", "metric", "period_key", name="usage_counters_user_metric_period"
        ),
    )
