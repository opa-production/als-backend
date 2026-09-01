import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDelete, Timestamps, UuidPK, UUIDPrimaryKey

#: Everything a material can be.
#:
#: ``note`` is text the student typed and ``link`` is a URL — neither has a file
#: and neither needs reading. ``pdf`` and ``image`` do.
MATERIAL_KINDS = ("note", "pdf", "image", "link")

#: The kinds that arrive as a file and have to be read before the tutor can
#: quote them.
#:
#: **This tuple is the contract between the upload endpoint and the extraction
#: queue, and it exists because they once disagreed.** ``/materials/upload-url``
#: accepted ``image`` and wrote the row as ``pending``; ``claim_batch`` selected
#: ``kind IN ('pdf')``. Nothing was wrong with either line on its own, so nothing
#: failed and nothing logged — every photo a student uploaded simply sat in
#: ``pending`` for ever, and the app, which has no way to tell "queued" from
#: "abandoned", showed "reading your notes" until the end of time.
#:
#: One tuple, imported by both, and `test_extraction.py` asserts that every kind
#: the API accepts is a kind the queue will claim. A row that nothing will ever
#: pick up should not be constructible.
EXTRACTABLE_KINDS = ("pdf", "image")


class Material(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """
    One thing filed under a unit: a note, a PDF, a photo, a link.

    This row is metadata only. The bytes live in Supabase Storage and the text
    pulled out of them lives in ``material_chunks`` — see ARCHITECTURE.md §2.
    A 40 MB slide deck in a column here would bloat every backup and hold a
    pooled connection open for the length of a download.
    """

    __tablename__ = "materials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: One of `MATERIAL_KINDS`. Not a database enum: adding a kind would then
    #: be a migration, and the values are validated where they enter.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="note")
    title: Mapped[str] = mapped_column(String(300), nullable=False)

    #: Text the student typed or pasted. For a PDF this stays empty and the
    #: extracted text goes to chunks instead, so one column never means two
    #: different things depending on the row.
    body: Mapped[str] = mapped_column(Text, default="")

    # --- Storage ----------------------------------------------------------
    #: Bucket-relative path, never a signed URL. URLs expire; paths do not.
    storage_bucket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Known only once a worker has opened the file. This is what finally makes
    #: the plans' page limits enforceable — nothing on the device can count the
    #: pages in a PDF, which is why those limits are advertised but unchecked
    #: in the app today.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Extraction -------------------------------------------------------
    #: pending | running | done | failed | skipped
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending")
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: When the student was told this finished, or ``None`` if they have not
    #: been. Set once, on the first sweep that picks the material up after it
    #: reaches a terminal status.
    #:
    #: A column rather than a dedupe key computed from the row, because the
    #: notification is *coalesced* — four photos filed in one sitting are one
    #: notification naming the unit, not four buzzes, which is how somebody
    #: turns notifications off for the whole app. There is no stable key for
    #: "this group of four", so the fact of having told them is recorded per
    #: material instead.
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    chunks: Mapped[list["MaterialChunk"]] = relationship(
        back_populates="material",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_materials_user_updated", "user_id", "updated_at"),
        Index("ix_materials_unit_archived", "unit_id", "archived"),
        # The queue the extraction worker pulls from.
        Index("ix_materials_extraction", "extraction_status", "created_at"),
    )


class MaterialChunk(Base, UUIDPrimaryKey, Timestamps):
    """
    A passage of readable text, and where it came from.

    The tutor answers by quoting, so every answer has to be traceable back to a
    page. Chunking at ingest rather than at query time means the expensive work
    happens once in a worker instead of on every question.

    An embedding column belongs here when device-side retrieval outgrows
    itself — ``embedding: Mapped[list[float]] = mapped_column(Vector(1536))``
    with pgvector. Laid out for it; not carrying it yet.
    """

    __tablename__ = "material_chunks"

    material_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Denormalised from the material so every query can be scoped to one
    #: student without a join. Retrieval is the hottest read in the system.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidPK, index=True, nullable=False
    )

    #: Position within the material, so chunks reassemble in order.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 1-based; null for anything without pages. This is the "exact page" the
    #: paid tiers promise in their citations.
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    material: Mapped["Material"] = relationship(back_populates="chunks", lazy="raise")

    __table_args__ = (
        Index("ix_material_chunks_material_ordinal", "material_id", "ordinal"),
    )
