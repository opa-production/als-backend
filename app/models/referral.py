import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class ReferralReward(Base, UUIDPrimaryKey, Timestamps):
    """
    One referral that turned into a payment, and what it is worth.

    A row is written the moment the person who was referred pays for the first
    time — never at sign-up. That single rule is what makes the programme
    unfarmable: the cheapest way to attack it is to spend real money, and
    nobody spends KES 150 to earn KES 30 of tokens.

    The row is also the ledger. "Why did I not get my free days" is a support
    question that has to be answerable, so a referral that was refused is
    recorded as ``voided`` with a reason rather than silently not written.

    Four states, and the middle one is the interesting one:

    ``pending``   Earned, inside the seven-day hold. A payment that reverses in
                  that window costs nothing, because nothing has been credited.
    ``banked``    Vested, but the referrer is on Free and has no plan to add
                  days to. Held until they subscribe, which is the point: a
                  student sitting on four earned weeks has a much better reason
                  to pay than one looking at a paywall.
    ``credited``  Days added to a subscription. Terminal, and never reversed —
                  taking back days somebody has already used is a support
                  ticket you lose.
    ``voided``    Refused, expired, or clawed back before vesting. ``reason``
                  says which.
    """

    __tablename__ = "referral_rewards"

    #: Who earns. Not cascade-deleted with the person they referred: the reward
    #: is theirs and survives the other account.
    referrer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Who paid. Nullable so a deleted account leaves the ledger standing —
    #: the days were earned and the row has to keep explaining them.
    referred_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    #: pending | banked | credited | voided
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: Why it was voided, or which rule refused it. Empty on a healthy row.
    reason: Mapped[str] = mapped_column(String(80), default="")

    #: What the referrer gets. Days, not money — see `app/services/referrals.py`.
    days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: What the person who paid got at the same moment, as the referrer's gift.
    #: Recorded here rather than inferred, so a support question about either
    #: side of the exchange is answered by one row.
    friend_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The plan that was bought. A Season is four months of revenue arriving at
    #: once and pays better, so this is worth keeping beside the number.
    tier: Mapped[str] = mapped_column(String(16), default="")

    #: When the hold ends and this becomes creditable.
    vest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When a banked reward stops being worth anything. An open promise on an
    #: account that may never convert is a liability with no end date.
    banked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One reward per person referred, ever. Without this a friend who
        # cancels and buys again pays the referrer twice for one introduction.
        UniqueConstraint("referred_user_id", name="referral_rewards_referred"),
        # The sweep's query: everything pending whose hold has expired.
        Index("ix_referral_rewards_status_vest", "status", "vest_at"),
        Index("ix_referral_rewards_referrer_created", "referrer_id", "created_at"),
    )
