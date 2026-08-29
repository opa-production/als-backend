import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class NotificationLog(Base, UUIDPrimaryKey, Timestamps):
    """
    One nudge that was sent, or tried.

    This table exists for one reason: a reminder must go out *once*. The sweep
    runs every minute, a deadline sits inside its lead window for the whole of
    that window, and two workers may overlap during a deploy — without a record
    of what has already gone, a student gets the same "due in an hour" fifteen
    times.

    ``dedupe_key`` is what enforces that, not the timestamps. It names the thing
    being reminded about *and the occurrence*: an event's key carries its due
    date, a class's carries the local day. So moving a deadline earns a fresh
    nudge, while the same deadline swept a hundred times does not.

    Failures are recorded too, with ``status``. A row that says ``failed`` is
    the difference between "Expo rejected this token" and the silence that
    otherwise looks exactly like a notification nobody tapped.
    """

    __tablename__ = "notification_log"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: deadline | class | test
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Stable per occurrence — see the class docstring. Unique per user.
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)

    title: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(String(300), default="")

    #: sent | failed
    status: Mapped[str] = mapped_column(String(16), default="sent")
    #: Empty unless something went wrong. Expo's own error string, truncated.
    error: Mapped[str] = mapped_column(String(300), default="")

    #: When the reminder was *for*, not when it was sent. Kept so "the 8am
    #: lecture nudge arrived at 8:40" is answerable from this table alone.
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # The whole point of the table. An insert that collides is a reminder
        # that has already gone out.
        UniqueConstraint("user_id", "dedupe_key", name="notification_log_user_key"),
        Index("ix_notification_log_user_created", "user_id", "created_at"),
    )
