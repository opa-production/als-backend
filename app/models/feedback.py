import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class FeatureRequest(Base, UUIDPrimaryKey, Timestamps):
    """
    One thing a student asked for, in their own words.

    A paragraph and nothing else. Every field this table does not have was
    considered and left out: a title makes people write a headline instead of
    the problem, a category makes them pick a box we invented before we knew
    what the boxes are, and votes make it a forum to be moderated. What is
    wanted here is the sentence someone types when the app cannot do the thing
    they came to do, and anything else on the form costs a share of the people
    who would otherwise have sent it.

    Nothing is ever shown back to other students, so there is no ranking, no
    status and no reply. If a request is worth answering, it is answered by
    building the thing.

    A hard row rather than a soft-deleted one, and no ``SoftDelete``: this is
    not the student's content in the sense that notes are. It never syncs to
    the device, and deleting the account takes it with them by cascade.
    """

    __tablename__ = "feature_requests"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: The whole submission. ``Text`` rather than a bounded column even though
    #: the API caps the length: the cap is a product decision that will move,
    #: and a migration to widen a column is a worse place to discover that than
    #: a constant in a schema.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    #: What they were running when they wrote it, if the app said. Sent by the
    #: client, trusted for nothing — it is here so "the timetable is empty" can
    #: be read against a build where it was, rather than guessed at.
    app_version: Mapped[str] = mapped_column(String(32), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")

    __table_args__ = (
        # The console reads this newest-first and the throttle counts a
        # student's recent rows. Both are this index.
        Index("ix_feature_requests_user_created", "user_id", "created_at"),
        Index("ix_feature_requests_created", "created_at"),
    )
