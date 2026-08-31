"""
What the app on a student's phone should be running.

One row per published build, per platform. The newest published row for a
platform is the answer to "is there an update", and it also carries
``minimum_version`` — the oldest build still allowed to keep going.

Kept in the database rather than in config for one reason: forcing an update is
a decision made in a hurry, usually because something is broken in a build that
is already on ten thousand phones. That has to be one field on a console screen,
not an environment variable, a redeploy, and a restart.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class AppRelease(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "app_releases"

    #: ``ios`` or ``android``. Not an enum column: the set is stable, and a
    #: check constraint here would need a migration the day a web build wants
    #: the same treatment. Validated at the edge instead.
    platform: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Dotted numbers, as the store shows them — "1.4.0". Compared numerically
    #: rather than as text, which is the whole reason `app/services/releases.py`
    #: exists: "1.10.0" is newer than "1.9.0" and sorts before it as a string.
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The oldest build still allowed to run. Anything below it is told to
    #: update and given no way past the modal.
    #:
    #: Lives on the release rather than in its own table because it is only
    #: ever read from the newest published row — "what does the current release
    #: require" — and a separate table would be a second thing to remember to
    #: set.
    minimum_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    #: Where to send someone who taps Update. Falls back to the store URL in
    #: settings when empty, so the usual case is one field nobody fills in.
    store_url: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    #: Shown in the modal. Written for a student, not a changelog: "quizzes no
    #: longer lose your place" beats "fix: quiz state persistence".
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    #: Whether this build is being offered yet. A row can exist — with notes
    #: written and reviewed — before the store has finished rolling it out,
    #: and offering an update that is not downloadable yet is worse than
    #: offering none.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # One row per build. Publishing "1.4.0" twice is a mistake, and two
        # rows claiming to be the same version is a coin toss over which notes
        # a student sees.
        UniqueConstraint("platform", "version", name="uq_app_releases_platform_version"),
        Index("ix_app_releases_platform_published", "platform", "published"),
    )
