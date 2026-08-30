import uuid
from datetime import date, time
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, SmallInteger, String, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDelete, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.account import User


class Unit(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """
    One subject: CS201, MAT204.

    Everything a student files hangs off a unit, which is why the delete is
    soft — a hard delete would cascade a semester of notes out of existence
    from one mis-tap on a phone that is offline and cannot be asked to confirm.
    """

    __tablename__ = "units"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    lecturer: Mapped[str] = mapped_column(String(160), default="")

    user: Mapped["User"] = relationship(back_populates="units", lazy="raise")

    __table_args__ = (
        # A student cannot have two CS201s. Scoped to the user, obviously —
        # every student in the country has one.
        UniqueConstraint("user_id", "code", name="units_user_code"),
        Index("ix_units_user_updated", "user_id", "updated_at"),
    )


class ClassSession(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """
    A recurring slot on the timetable.

    Stored as a weekday plus wall-clock times, not as dated occurrences: a
    lecture is "Tuesdays at 8" for a whole semester, and materialising every
    instance would be thousands of rows that all have to change when the room
    does.

    Wall clock, not UTC. 08:00 means 08:00 where the student is; converting to
    UTC would drag a lecture across a DST boundary the printed timetable never
    had.
    """

    __tablename__ = "class_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: 0 = Sunday, matching JavaScript's getDay() so the client needs no
    #: translation layer and no off-by-one is ever possible at the boundary.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    starts_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    ends_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    room: Mapped[str] = mapped_column(String(80), default="")

    @staticmethod
    def weekday_of(day: date) -> int:
        """
        A calendar date as this column numbers it.

        Python counts from Monday and this column counts from Sunday, so the
        two are never directly comparable. The conversion lives here, next to
        the column it converts for — written out at each call site it is an
        off-by-one waiting for the one place somebody forgets.
        """
        return (day.weekday() + 1) % 7

    __table_args__ = (Index("ix_class_sessions_user_weekday", "user_id", "weekday"),)
