import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JsonB, SoftDelete, Timestamps, UuidPK, UUIDPrimaryKey


class Chat(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """One conversation, optionally scoped to a unit."""

    __tablename__ = "chats"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: SET NULL, not CASCADE: dropping a unit should not delete the answers a
    #: student got out of it while they still had it.
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("units.id", ondelete="SET NULL"), index=True, nullable=True
    )

    #: The first question asked, trimmed. A student recognises their own words
    #: in a list long before they recognise a date.
    title: Mapped[str] = mapped_column(String(120), default="New chat")

    messages: Mapped[list["Message"]] = relationship(
        back_populates="chat",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_chats_user_updated", "user_id", "updated_at"),)


class Message(Base, UUIDPrimaryKey, Timestamps):
    """
    One turn in a conversation.

    ``sources`` is JSONB rather than a join table: it is written once, read
    with its message and never queried across. A table would mean three joins
    to render a thread, for nothing.
    """

    __tablename__ = "messages"

    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidPK, index=True, nullable=False
    )

    #: student | tutor
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: [{ material_id, title, page_number }] — exactly what the answer quoted.
    sources: Mapped[list | None] = mapped_column(JsonB, nullable=True)

    #: Cost accounting for the paid tiers, and the only way to learn what a
    #: heavy user actually costs before the monthly bill explains it.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    chat: Mapped["Chat"] = relationship(back_populates="messages", lazy="raise")

    __table_args__ = (Index("ix_messages_chat_created", "chat_id", "created_at"),)
