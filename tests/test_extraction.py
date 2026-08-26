"""
The extraction pipeline.

This is the step the tutor's whole grounding story rests on. Without it
`material_chunks` stays empty, retrieval finds nothing, and every coursework
question is answered "I could not find this in your material" — technically
honest and completely useless.

What is worth pinning:

* Text comes out with its **page numbers**, because that is what a citation is.
* Chunks **overlap**, so a definition split across a boundary is still findable.
* A **scan** fails with a sentence a student could act on, not a stack trace.
* Re-running **replaces** rather than duplicates.
* Page limits are enforced *after* the count is known and *before* anything is
  indexed.
"""

import io
import itertools
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.course import Unit
from app.models.knowledge import Material, MaterialChunk
from app.services.plans import Tier
from app.workers import extraction
from tests.conftest import sign_in

_CODE = itertools.count(201)


def _next_code() -> int:
    return next(_CODE)


def _pdf(pages: list[str]) -> bytes:
    """A real PDF, built in memory. No fixture files to keep in the repo."""
    from pypdf import PdfWriter

    try:
        from reportlab.lib.pagesizes import A4  # noqa: F401
    except ImportError:
        pytest.skip("reportlab is not installed; PDF fixtures cannot be built")

    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer)
    for text in pages:
        # Wrapped by hand: reportlab does not do it, and one long line off the
        # edge of the page extracts as nothing.
        y = 800
        for i in range(0, len(text), 90):
            drawing.drawString(60, y, text[i : i + 90])
            y -= 14
        drawing.showPage()
    drawing.save()

    buffer.seek(0)
    writer = PdfWriter(clone_from=buffer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def _material(client, user_id, *, kind="pdf", path="u/m/file.pdf") -> uuid.UUID:
    """
    One material under a unit.

    The unit code is derived from a counter because `units` has a
    UNIQUE(user_id, code): a test filing two documents for one student would
    otherwise collide on the second.
    """
    async with client.sessions() as session:
        code = f"CS{_next_code()}"
        unit = Unit(id=uuid.uuid4(), user_id=user_id, code=code, title="Operating Systems")
        session.add(unit)
        await session.flush()

        material = Material(
            id=uuid.uuid4(),
            user_id=user_id,
            unit_id=unit.id,
            kind=kind,
            title="Week 4 notes",
            storage_bucket="materials",
            storage_path=path,
            extraction_status="pending",
        )
        session.add(material)
        await session.commit()
        return material.id


def _client_serving(data: bytes, *, status: int = 200) -> httpx.AsyncClient:
    """
    A client that answers the signed-URL request and the download.

    Storage is mocked at the transport rather than the adapter so the real
    `SupabaseStorage` code path is exercised — including the guard that refuses
    when the keys are missing.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if "/object/sign/" in str(request.url):
            return httpx.Response(200, json={"signedURL": "/object/signed/file.pdf"})
        return httpx.Response(status, content=data)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def _storage_configured(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_key", "service-key")


# --- Reading ------------------------------------------------------------------


def test_text_comes_out_with_its_page_numbers():
    """Page numbers are what makes a citation checkable."""
    data = _pdf(
        [
            "Deadlock is a standstill between two or more processes.",
            "Starvation is a different problem, where one process never runs.",
        ]
    )

    result = extraction._read_pdf(data)

    assert result.page_count == 2
    assert [page for page, _ in result.pages] == [1, 2]
    assert "Deadlock" in result.pages[0][1]
    assert "Starvation" in result.pages[1][1]


def test_a_scan_fails_with_something_a_student_can_act_on():
    """
    A PDF of photographs has pages and no text.

    "No text could be read" plus what to do about it beats a parser exception
    the student will never see the inside of.
    """
    data = _pdf(["", "", ""])

    with pytest.raises(extraction.ExtractionError) as caught:
        extraction._read_pdf(data)

    assert "scan" in str(caught.value).lower()
    assert "OCR" in str(caught.value)


def test_rubbish_is_not_mistaken_for_a_pdf():
    with pytest.raises(extraction.ExtractionError):
        extraction._read_pdf(b"this is not a pdf at all")


def test_hyphenated_line_breaks_are_rejoined():
    """
    PDF extraction preserves the hyphen a typesetter used to break a word.

    "data-\\nbase" does not match a search for "database", which is most of what
    retrieval does.
    """
    assert "database" in extraction._tidy("a data-\nbase system")
    # A real hyphenated word keeps its hyphen.
    assert "state-of-the-art" in extraction._tidy("a state-of-the-art result")


# --- Chunking -----------------------------------------------------------------


def test_chunks_never_span_two_pages():
    """A chunk with two page numbers cannot be cited."""
    pages = [(1, "alpha " * 400), (2, "beta " * 400)]

    chunks = extraction.chunk_pages(pages)

    for page_number, content in chunks:
        assert page_number in (1, 2)
        if page_number == 1:
            assert "beta" not in content
        else:
            assert "alpha" not in content


def test_chunks_overlap_so_a_split_definition_is_still_findable():
    """
    Without overlap, a definition cut across a boundary is retrievable by
    neither half: the first ends mid-sentence, the second starts with a pronoun.
    """
    text = " ".join(f"word{i}" for i in range(600))
    chunks = extraction.chunk_pages([(1, text)])

    assert len(chunks) > 1
    first, second = chunks[0][1], chunks[1][1]
    tail = first.split()[-5:]
    assert any(word in second for word in tail), "consecutive chunks share no text"


def test_a_short_page_becomes_one_chunk():
    chunks = extraction.chunk_pages([(3, "A short but complete paragraph about deadlock.")])

    assert len(chunks) == 1
    assert chunks[0][0] == 3


def test_near_empty_pages_are_left_out_of_the_index():
    """A page number and a footer cannot answer anything, and dilute ranking."""
    assert extraction.chunk_pages([(1, "12")]) == []


# --- The job ------------------------------------------------------------------


async def test_a_pdf_becomes_searchable_chunks(client):
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)
    data = _pdf(["Deadlock occurs when two processes each hold what the other needs."])

    async with client.sessions() as session, _client_serving(data) as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "done"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        chunks = (
            await session.scalars(
                select(MaterialChunk).where(MaterialChunk.material_id == material_id)
            )
        ).all()

    assert material.extraction_status == "done"
    assert material.page_count == 1
    assert chunks
    assert chunks[0].page_number == 1
    assert chunks[0].user_id == user_id, "chunks carry the owner for retrieval scoping"
    assert "Deadlock" in chunks[0].content


async def test_the_tutor_can_now_ground_an_answer(client):
    """
    The point of the whole pipeline, end to end.

    Retrieval finds nothing before extraction and grounds an answer after it.
    """
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)
    data = _pdf(["The four Coffman conditions are mutual exclusion, hold and wait."])

    from app.services.ai import retrieval

    async with client.sessions() as session:
        before = await retrieval.search(
            session, user_id=user_id, question="Coffman conditions"
        )
    assert before == [], "nothing is searchable until extraction has run"

    async with client.sessions() as session, _client_serving(data) as http:
        await extraction.extract_material(session, material_id, client=http)

    async with client.sessions() as session:
        after = await retrieval.search(
            session, user_id=user_id, question="Coffman conditions"
        )

    assert after and retrieval.is_grounded(after)
    assert after[0].page_number == 1


async def test_re_running_replaces_rather_than_duplicates(client):
    """
    A retry after a crash must not double every passage in the index.

    Delete-then-insert rather than upsert-on-ordinal: a second run can produce
    fewer chunks, and leftovers from a longer one would be quoted as if they
    were still in the document.
    """
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)
    data = _pdf(["Deadlock is a standstill between two or more processes."])

    for _ in range(2):
        async with client.sessions() as session, _client_serving(data) as http:
            await extraction.extract_material(session, material_id, client=http)

    async with client.sessions() as session:
        count = len(
            (
                await session.scalars(
                    select(MaterialChunk).where(MaterialChunk.material_id == material_id)
                )
            ).all()
        )

    assert count == 1


async def test_a_broken_file_is_recorded_not_retried_forever(client):
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session, _client_serving(b"not a pdf") as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "failed"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)

    assert material.extraction_status == "failed"
    assert material.extraction_error
    # `failed` is not `pending`, so the queue will not pick it up again.
    async with client.sessions() as session:
        assert material_id not in await extraction.claim_batch(session, 10)


async def test_a_download_failure_does_not_lose_the_material(client):
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session, _client_serving(b"", status=500) as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "failed"


async def test_a_document_over_the_plan_limit_indexes_nothing(client, monkeypatch):
    """
    The limit is checked after the page count is known and before anything is
    written, so an oversized document leaves no trace in the index.

    Marked `skipped`, not `failed`: the file is fine, the allowance is not, and
    the console has to be able to tell those apart.
    """
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session:
        from app.services.billing import activate

        await activate(session, user_id=user_id, tier=Tier.TRIAL, verified=True)
        await session.commit()

    # The trial allows 30 pages in one file.
    data = _pdf([f"Page {i} of a very long document about deadlock." for i in range(40)])

    async with client.sessions() as session, _client_serving(data) as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "skipped"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        chunks = (
            await session.scalars(
                select(MaterialChunk).where(MaterialChunk.material_id == material_id)
            )
        ).all()

    assert material.extraction_status == "skipped"
    assert chunks == [], "an over-limit document must leave nothing behind"


async def test_a_deleted_material_is_skipped(client):
    """A student who deletes a file before the worker reaches it is obeyed."""
    from app.core.clock import now as utc_now

    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        material.deleted_at = utc_now()
        await session.commit()

    async with client.sessions() as session, _client_serving(_pdf(["text"])) as http:
        assert await extraction.extract_material(session, material_id, client=http) == "gone"


# --- The queue ----------------------------------------------------------------


async def test_the_queue_takes_pending_pdfs_oldest_first(client):
    headers, user_id = await sign_in(client)
    first = await _material(client, user_id)
    second = await _material(client, user_id)

    async with client.sessions() as session:
        claimed = await extraction.claim_batch(session, 10)

    assert claimed[:2] == [first, second]


async def test_notes_and_links_are_not_queued(client):
    """They carry their text already; there is nothing to extract."""
    headers, user_id = await sign_in(client)
    await _material(client, user_id, kind="note")
    await _material(client, user_id, kind="link")

    async with client.sessions() as session:
        assert await extraction.claim_batch(session, 10) == []


async def test_work_stranded_by_a_dead_worker_is_picked_up_again(client):
    """
    A process killed between `running` and `done` leaves a row nothing will
    ever claim, because the queue only looks at `pending`.
    """
    from datetime import timedelta

    from app.core.clock import now as utc_now

    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        material.extraction_status = "running"
        material.updated_at = utc_now() - timedelta(hours=1)
        await session.commit()

    async with client.sessions() as session:
        assert await extraction.claim_batch(session, 10) == []
        assert await extraction.requeue_stalled(session) == 1

    async with client.sessions() as session:
        assert material_id in await extraction.claim_batch(session, 10)


async def test_a_worker_still_running_is_left_alone(client):
    """Requeueing live work would have two workers on the same document."""
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        material.extraction_status = "running"
        await session.commit()

    async with client.sessions() as session:
        assert await extraction.requeue_stalled(session) == 0
