import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class UserSettings(Base, UUIDPrimaryKey, Timestamps):
    """
    Preferences, one row per student.

    A table rather than columns on ``users`` because these sync on their own
    cadence and change far more often than a profile does — and because a
    preference added later is a nullable column on a small table instead of a
    migration against the row every request already loads.
    """

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # --- Notifications ----------------------------------------------------
    deadline_reminders: Mapped[bool] = mapped_column(Boolean, default=True)
    class_reminders: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Minutes before a class or deadline to send the nudge.
    reminder_lead_minutes: Mapped[int] = mapped_column(default=15)

    #: Nothing is sent inside this window. Stored as "HH:MM" strings rather
    #: than times, because they are a wall-clock preference in the student's
    #: own timezone, not an instant.
    quiet_hours_start: Mapped[str] = mapped_column(String(5), default="22:00")
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default="06:00")

    #: IANA name, e.g. "Africa/Nairobi". Without it the server cannot know
    #: when 22:00 is for this person, and every reminder lands at the wrong
    #: hour for anyone who travels.
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Nairobi")

    # --- Security ---------------------------------------------------------
    #: Guards the *view* on the device, not the data here. A borrowed phone
    #: should not show someone's coursework; that is all this claims.
    biometric_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    #: What the device reported it can do — "face", "fingerprint", or empty.
    biometric_kind: Mapped[str] = mapped_column(String(16), default="")


class StudyDay(Base, UUIDPrimaryKey, Timestamps):
    """
    One day on which a student actually revised.

    A row per day rather than a running counter, because a counter cannot
    answer "which days of this week" — which is the whole streak screen — and
    cannot be recomputed if it ever drifts. Days are cheap: a heavy user
    generates 365 rows a year.

    The unique constraint is what makes recording idempotent. Asking three
    questions in an evening is one day of studying, and the client can post
    freely without counting first.
    """

    __tablename__ = "study_days"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: The student's *local* day, sent by the device. Deriving it from a UTC
    #: timestamp would break a streak for anyone revising after 3am, which is
    #: precisely the population this app is for.
    day: Mapped[date] = mapped_column(Date, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "day", name="study_days_user_day"),
        Index("ix_study_days_user_day", "user_id", "day"),
    )
