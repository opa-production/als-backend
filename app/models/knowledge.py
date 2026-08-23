import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDelete, Timestamps, UuidPK, UUIDPrimaryKey


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

    #: note | pdf | image | link
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
