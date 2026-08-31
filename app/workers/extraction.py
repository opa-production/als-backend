"""
Turning an uploaded file into text the tutor can quote.

This is the step everything else has been waiting on. Without it
`material_chunks` is empty, retrieval finds nothing, and the tutor answers every
coursework question with "I could not find this in your material" — which is
technically honest and completely useless.

The shape, and why:

* **The file goes to Storage, the text goes to Postgres.** ARCHITECTURE.md §2.
  A student searches text, so the text has to be somewhere searchable; the
  40 MB it came from does not belong in a row.
* **Parsing happens off the event loop.** `pypdf` is CPU-bound and synchronous.
  Called directly in an async worker it would stall every other coroutine in the
  process for the length of a 200-page document, so it goes through
  `asyncio.to_thread`.
* **Page numbers are carried through.** They are what makes a citation a
  citation. A chunk that has lost track of its page can still be quoted, but the
  student cannot go and check it, which is most of the value.
* **The page limits are enforced here, and only here.** Nothing on a phone can
  count the pages in a PDF, which is why `maxSingleFilePages` and
  `totalPdfPagesPool` are advertised on the pricing card and unchecked in the
  app. This is the first moment the real number is known.
"""

from __future__ import annotations

import asyncio
import io
import re
import uuid
from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now as utc_now
from app.core.errors import QuotaExceeded
from app.models.knowledge import Material, MaterialChunk
from app.services.quota import check_pdf_pages, get_entitlement, record_pdf_pages
from app.services.storage import Bucket, StorageError, SupabaseStorage

log = structlog.get_logger()

#: Roughly a page of prose. Big enough to hold a whole idea — a definition and
#: the sentence that qualifies it — and small enough that quoting one does not
#: bury the relevant line in a wall of text.
CHUNK_CHARS = 1200

#: How much of the previous chunk each one repeats.
#:
#: Without it, a definition split across a boundary is retrievable by neither
#: half: the first ends mid-sentence and the second starts with a pronoun whose
#: referent is in the chunk before. The cost is about a sixth more rows.
CHUNK_OVERLAP = 200

#: A page with less than this much text is almost always a scan, a cover, or a
#: diagram. Kept out of the index because it cannot answer anything and dilutes
#: the ranking of pages that can.
MIN_PAGE_CHARS = 40

#: A hard ceiling independent of the plan limits, which are about fairness.
#: This one is about the process: a 2,000-page book would hold a worker for
#: minutes and produce tens of thousands of rows.
MAX_PAGES = 600


@dataclass
class Extracted:
    """The result of reading one document."""

    page_count: int
    #: (page number, text) for pages that actually had text on them.
    pages: list[tuple[int, str]]

    @property
    def text_pages(self) -> int:
        return len(self.pages)


class ExtractionError(Exception):
    """
    A document that cannot be read.

    Distinct from a bug: a corrupt upload, an encrypted PDF or a pure scan are
    all *expected*, and each ends with the material marked `failed` and a
    sentence a student could act on rather than a stack trace in the journal.
    """


# --- Reading ------------------------------------------------------------------


def _read_pdf(data: bytes) -> Extracted:
    """
    Text per page.

    Synchronous and CPU-bound on purpose — the caller puts it on a thread. Doing
    that here would hide it from the one place it matters.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as error:
        raise ExtractionError("That file could not be opened as a PDF.") from error
    except Exception as error:  # noqa: BLE001 — pypdf raises a wide variety
        raise ExtractionError("That file could not be read.") from error

    if reader.is_encrypted:
        # Some PDFs are "encrypted" with an empty owner password, which pypdf
        # can open. Worth trying before refusing a file the student can read
        # perfectly well on their own laptop.
        try:
            if reader.decrypt("") == 0:
                raise ExtractionError("That PDF is password protected.")
        except ExtractionError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ExtractionError("That PDF is password protected.") from error

    page_count = len(reader.pages)
    if page_count == 0:
        raise ExtractionError("That PDF has no pages.")
    if page_count > MAX_PAGES:
        raise ExtractionError(
            f"That document has {page_count} pages. The limit is {MAX_PAGES}."
        )

    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            # One unreadable page must not lose the other 199. A malformed font
            # table or a broken content stream is common in scanned-then-OCRed
            # documents and affects single pages.
            log.warning("extraction_page_failed", page=index)
            continue

        cleaned = _tidy(text)
        if len(cleaned) >= MIN_PAGE_CHARS:
            pages.append((index, cleaned))

    if not pages:
        raise ExtractionError(
            "No text could be read from that document. It looks like a scan — "
            "photographs of pages need OCR, which is a Synapse feature."
        )

    return Extracted(page_count=page_count, pages=pages)


_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
#: A word broken across a line by a hyphen, which PDF text extraction preserves
#: and which then fails to match the word a student searched for.
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def _tidy(text: str) -> str:
    """
    PDF text as prose.

    Extraction produces hard line breaks at the column width, hyphenated word
    splits, and runs of spaces used for layout. All three break keyword search:
    "data-\\nbase" does not match "database", and neither does "d a t a b a s e".
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


# --- Chunking -----------------------------------------------------------------


def chunk_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """
    (page number, passage), in reading order.

    Chunks never span pages. They could — a paragraph continuing across a page
    break is one idea — but a chunk with two page numbers cannot be cited, and a
    citation the student cannot open is worth less than the sentence of context
    that was lost.
    """
    chunks: list[tuple[int, str]] = []

    for page_number, text in pages:
        text = text.strip()

        # The same floor the long path applies further down. Without it here, a
        # page holding only a folio number becomes a chunk — `_read_pdf` filters
        # those out before this is reached in the normal flow, but this function
        # is public and must not depend on having been called from there.
        if len(text) < MIN_PAGE_CHARS:
            continue

        if len(text) <= CHUNK_CHARS:
            chunks.append((page_number, text))
            continue

        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))

            # Prefer to break at a sentence, then at a word. Cutting mid-word
            # leaves a fragment that matches nothing.
            if end < len(text):
                window = text[start:end]
                for boundary in (". ", "\n", " "):
                    found = window.rfind(boundary)
                    if found > CHUNK_CHARS // 2:
                        end = start + found + len(boundary)
                        break

            piece = text[start:end].strip()
            if len(piece) >= MIN_PAGE_CHARS:
                chunks.append((page_number, piece))

            if end >= len(text):
                break
            start = max(start + 1, end - CHUNK_OVERLAP)

    return chunks


# --- The job ------------------------------------------------------------------


async def extract_material(
    session: AsyncSession, material_id: uuid.UUID, *, client: httpx.AsyncClient
) -> str:
    """
    Read one material and index it. Returns the status it ended in.

    Idempotent: re-running replaces the chunks rather than adding a second copy,
    so a retry after a crash is safe and a re-upload does not double every
    passage in the index.
    """
    material = await session.get(Material, material_id)
    if material is None or material.deleted_at is not None:
        return "gone"

    material.extraction_status = "running"
    await session.commit()

    try:
        data = await _download(material, client)
        extracted = await asyncio.to_thread(_read_pdf, data)

        entitlement = await get_entitlement(session, material.user_id)
        # After extraction, because until now nobody knew the page count — and
        # before writing anything, because a document over the limit should
        # leave no trace in the index.
        await check_pdf_pages(session, material.user_id, entitlement, extracted.page_count)

        chunks = chunk_pages(extracted.pages)
        await _replace_chunks(session, material, chunks)

        material.page_count = extracted.page_count
        material.extraction_status = "done"
        material.extraction_error = None
        await record_pdf_pages(session, material.user_id, extracted.page_count)
        await session.commit()

        log.info(
            "extraction_done",
            material_id=str(material_id),
            pages=extracted.page_count,
            text_pages=extracted.text_pages,
            chunks=len(chunks),
        )
        return "done"

    except QuotaExceeded as error:
        # Not a failure of the document. Recorded separately so the console can
        # tell "this student is over their allowance" from "this file is
        # broken", which need completely different responses.
        return await _fail(session, material, error.message, status="skipped")

    except ExtractionError as error:
        return await _fail(session, material, str(error))

    except StorageError as error:
        return await _fail(session, material, str(error))

    except Exception:  # noqa: BLE001
        log.exception("extraction_crashed", material_id=str(material_id))
        return await _fail(session, material, "Something went wrong reading that file.")


async def _download(material: Material, client: httpx.AsyncClient) -> bytes:
    """The bytes, through a signed URL rather than the service key."""
    if not material.storage_path or not material.storage_bucket:
        raise ExtractionError("That upload never finished.")

    storage = SupabaseStorage(client)
    url = await storage.signed_download_url(
        Bucket(material.storage_bucket), material.storage_path
    )

    response = await client.get(url, timeout=60.0)
    if response.status_code >= 400:
        raise StorageError(f"Could not fetch that file ({response.status_code}).")

    return response.content


async def _replace_chunks(
    session: AsyncSession, material: Material, chunks: list[tuple[int, str]]
) -> None:
    """
    Swap the index for this material.

    Delete-then-insert in one transaction rather than an upsert on ordinal: a
    re-extraction can produce a different number of chunks, and leftovers from a
    longer previous run would be quoted as if they were still in the document.
    """
    await session.execute(
        delete(MaterialChunk).where(MaterialChunk.material_id == material.id)
    )

    for ordinal, (page_number, content) in enumerate(chunks):
        session.add(
            MaterialChunk(
                id=uuid.uuid4(),
                material_id=material.id,
                # Denormalised from the material so retrieval can scope to one
                # student without a join. See retrieval.py.
                user_id=material.user_id,
                ordinal=ordinal,
                page_number=page_number,
                content=content,
            )
        )

    await session.flush()


async def _fail(
    session: AsyncSession, material: Material, message: str, *, status: str = "failed"
) -> str:
    material.extraction_status = status
    material.extraction_error = message[:500]
    await session.commit()

    log.warning(
        "extraction_failed",
        material_id=str(material.id),
        status=status,
        reason=message[:200],
    )
    return status


# --- The queue ----------------------------------------------------------------


async def claim_batch(session: AsyncSession, limit: int) -> list[uuid.UUID]:
    """
    The next materials to read, oldest first.

    `SKIP LOCKED` is what lets two workers run without either doing the other's
    work — each takes rows the other has not locked instead of blocking on them.
    SQLite has no such thing and needs none: the tests run one worker.
    """
    statement = (
        select(Material.id)
        .where(
            Material.extraction_status == "pending",
            Material.deleted_at.is_(None),
            Material.kind.in_(("pdf",)),
        )
        .order_by(Material.created_at)
        .limit(limit)
    )

    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    if dialect == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    return list((await session.scalars(statement)).all())


async def requeue_stalled(session: AsyncSession, older_than_minutes: int = 15) -> int:
    """
    Put back anything a dead worker left mid-flight.

    A process killed between `running` and `done` leaves a row nothing will ever
    pick up again, because the queue only looks at `pending`. Without this the
    material is stuck forever and the student is never told why.
    """
    cutoff = utc_now().timestamp() - older_than_minutes * 60
    from datetime import UTC, datetime

    rows = (
        await session.scalars(
            select(Material).where(
                Material.extraction_status == "running",
                Material.updated_at < datetime.fromtimestamp(cutoff, UTC),
            )
        )
    ).all()

    for material in rows:
        material.extraction_status = "pending"

    if rows:
        await session.commit()
        log.warning("extraction_requeued", count=len(rows))

    return len(rows)
