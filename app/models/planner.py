import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDelete, Timestamps, UUIDPrimaryKey


class Event(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """
    Anything with a date: an assignment, a CAT, an exam, a project.

    ``unit_id`` is nullable on purpose — a scholarship deadline has a date and
    no lecturer, and forcing it under a unit is exactly how it gets lost.
    """

    __tablename__ = "events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), index=True, nullable=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: assignment | cat | exam | project | other
    kind: Mapped[str] = mapped_column(String(16), default="assignment")
    #: Only "other" carries one: what the student called an activity the fixed
    #: list has no name for.
    label: Mapped[str] = mapped_column(String(80), default="")

    #: An instant, not a date. The client sets it to 23:59 local, so the offset
    #: has to survive the round trip — store it naive and "due Friday" lands on
    #: Thursday for anyone east of UTC.
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_events_user_updated", "user_id", "updated_at"),
        # Drives both the reminder sweep and the client's upcoming list.
        Index("ix_events_user_due", "user_id", "done", "due_at"),
    )
