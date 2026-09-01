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
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.clock import as_utc
from app.core.config import settings
from app.models.course import Unit
from app.models.knowledge import Material, MaterialChunk
from app.services.ai import ocr
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


# --- Scans --------------------------------------------------------------------
#
# The path that did not exist. `allow_ocr_scans` and `monthly_ocr_page_limit`
# were on the pricing card and `check_ocr` was written and waiting, but nothing
# ever looked at an image — and because the queue selected only PDFs while the
# upload endpoint accepted images, every photo a student uploaded sat in
# `pending` for ever. No error, no log, no retry: the app showed "reading your
# notes" and meant it literally.


TRANSCRIPT = (
    "Deadlock requires four conditions holding at once: mutual exclusion, "
    "hold and wait, no preemption, and circular wait. Breaking any one of "
    "them is enough to prevent it."
)


def _scan_client(
    *, transcript: str = TRANSCRIPT, status: int = 200, image: bytes = b"\xff\xd8jpeg"
) -> httpx.AsyncClient:
    """
    Storage and the vision model, both mocked at the transport.

    Mocked here rather than at `ocr.read_image` so the real request is built and
    the real response parsed — the data URI, the message shape and the choices
    unwrapping are the parts most likely to be wrong, and stubbing the function
    would test none of them.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Matched on the endpoint shape, not a hostname: `OCR_BASE_URL` is
        # configuration, and a test that pins one provider's domain would go
        # red the day someone points it at another.
        if url.endswith("/chat/completions"):
            if status >= 400:
                return httpx.Response(status, json={"error": {"message": "no"}})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": transcript}}],
                    "usage": {"prompt_tokens": 800, "completion_tokens": 60},
                },
            )
        if "/object/sign/" in url:
            return httpx.Response(200, json={"signedURL": "/object/signed/photo.jpg"})
        return httpx.Response(200, content=image)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def _scan(client, user_id, *, mime="image/jpeg"):
    material_id = await _material(client, user_id, kind="image", path="u/m/photo.jpg")
    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        material.storage_bucket = "scans"
        material.mime_type = mime
        await session.commit()
    return material_id


async def _give(client, user_id, tier):
    from app.services.billing import activate

    async with client.sessions() as session:
        await activate(session, user_id=user_id, tier=tier, verified=True)
        await session.commit()


@pytest.fixture
def ocr_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", "test-key")
    monkeypatch.setattr(settings, "ocr_api_key", "")


async def test_every_kind_the_api_accepts_is_a_kind_the_queue_claims(
    client, ocr_configured
):
    """
    The invariant behind the original bug.

    The upload endpoint accepted images; the queue claimed only PDFs. Neither
    line was wrong on its own, nothing raised, and the row was simply
    unreachable for ever. A material the API will create must be a material the
    worker will pick up.
    """
    from app.api.v1.routes.materials import UploadUrlRequest
    from app.models.knowledge import EXTRACTABLE_KINDS

    accepted = UploadUrlRequest.model_fields["kind"].metadata[0].pattern
    for kind in EXTRACTABLE_KINDS:
        assert kind in accepted

    _, user_id = await sign_in(client)
    ids = {}
    for kind in EXTRACTABLE_KINDS:
        ids[kind] = await _material(client, user_id, kind=kind, path=f"u/m/f.{kind}")

    async with client.sessions() as session:
        claimed = set(await extraction.claim_batch(session, 50))

    for kind, material_id in ids.items():
        assert material_id in claimed, f"{kind} is accepted but never claimed"


async def test_a_photo_of_notes_becomes_searchable_text(client, ocr_configured):
    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client() as http:
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
    assert material.extraction_error is None
    # One photograph is one page, and it carries a page number so the tutor can
    # cite it the same way it cites a PDF.
    assert material.page_count == 1
    assert chunks and chunks[0].page_number == 1
    assert "circular wait" in chunks[0].content


async def test_a_scan_on_a_plan_without_ocr_is_skipped_not_failed(client, ocr_configured):
    """
    The file is fine; the allowance is not. The console has to tell those apart,
    and the student has to be told which one it was.
    """
    _, user_id = await sign_in(client)  # Free.
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client() as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "skipped"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)

    assert "Synapse" in material.extraction_error


async def test_the_allowance_is_not_spent_on_a_photo_that_could_not_be_read(
    client, ocr_configured
):
    """
    Checked before the vision call, spent after the whole job succeeds.

    A student whose photo came out blurred must not lose a page of their monthly
    thirty for a transcription they never received.
    """
    from app.services.quota import current_usage

    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client(transcript="NO_TEXT") as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "failed"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        spent = await current_usage(session, user_id, "ocr_pages")

    assert spent == 0
    assert "in focus" in material.extraction_error


async def test_a_successful_scan_does_spend_the_allowance(client, ocr_configured):
    from app.services.quota import current_usage

    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client() as http:
        await extraction.extract_material(session, material_id, client=http)

    async with client.sessions() as session:
        assert await current_usage(session, user_id, "ocr_pages") == 1


async def test_heic_is_refused_with_something_a_student_can_do(client, ocr_configured):
    """
    iPhones shoot HEIC by default and the vision API will not read it.

    Refused before the request, naming the actual setting to change — not sent
    and failed as an opaque provider error.
    """
    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id, mime="image/heic")

    async with client.sessions() as session, _scan_client() as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "failed"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)

    assert "Most Compatible" in material.extraction_error


async def test_scans_wait_rather_than_fail_when_ocr_is_not_configured(client, monkeypatch):
    """
    A missing key is our problem, not the file's.

    `failed` is terminal — the queue only looks at `pending` — so marking a
    perfectly good photo as broken because the box had no vision key would leave
    it wrong for ever after the key was added.
    """
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "ocr_api_key", "")

    _, user_id = await sign_in(client)
    pdf_id = await _material(client, user_id)
    scan_id = await _scan(client, user_id)

    async with client.sessions() as session:
        claimed = await extraction.claim_batch(session, 10)

    assert pdf_id in claimed
    assert scan_id not in claimed

    # And it is still there, waiting, once a key is set.
    monkeypatch.setattr(settings, "google_api_key", "test-key")
    async with client.sessions() as session:
        assert scan_id in await extraction.claim_batch(session, 10)


async def test_a_provider_outage_requeues_instead_of_rejecting(client, ocr_configured):
    """
    A five minute outage must not permanently reject an afternoon of uploads
    with an error the student cannot act on.
    """
    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client(status=429) as http:
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "pending"

    async with client.sessions() as session:
        material = await session.get(Material, material_id)
        assert material.extraction_status == "pending"
        # Back on the queue, not lost.
        assert material_id in await extraction.claim_batch(session, 10)


async def test_the_scan_request_goes_where_the_config_points(client, monkeypatch):
    """
    The provider is `OCR_BASE_URL`, not a hostname baked into the module.

    Worth pinning because getting it wrong is silent in exactly the way the
    original bug was: the request goes somewhere plausible, fails with a 401,
    and every scan is marked failed for a reason that has nothing to do with the
    student's photo.
    """
    monkeypatch.setattr(settings, "ocr_base_url", "https://elsewhere.test/v1/")
    monkeypatch.setattr(settings, "ocr_api_key", "explicit-key")
    monkeypatch.setattr(settings, "google_api_key", "should-not-be-used")
    monkeypatch.setattr(settings, "ocr_model", "some-vision-model")

    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    seen = {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/chat/completions"):
            seen["url"] = url
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": TRANSCRIPT}}]}
            )
        if "/object/sign/" in url:
            return httpx.Response(200, json={"signedURL": "/object/signed/photo.jpg"})
        return httpx.Response(200, content=b"\xff\xd8jpeg")

    async with (
        client.sessions() as session,
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
    ):
        status = await extraction.extract_material(session, material_id, client=http)

    assert status == "done"
    # The trailing slash on the base URL must not become a double slash.
    assert seen["url"] == "https://elsewhere.test/v1/chat/completions"
    # OCR_API_KEY wins over GOOGLE_API_KEY when both are set.
    assert seen["auth"] == "Bearer explicit-key"
    assert seen["body"]["model"] == "some-vision-model"

    # The image travels as a data URI carrying the material's own mime type —
    # send the wrong one and providers reject the whole request.
    parts = seen["body"]["messages"][0]["content"]
    image = next(part for part in parts if part["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_the_google_key_is_enough_on_its_own(client, monkeypatch):
    """
    The ordinary deployment sets one key. Asking for the same value in two
    variables is a way to have them disagree later.
    """
    monkeypatch.setattr(settings, "ocr_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "gemini-key")

    assert ocr.configured()

    _, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    async with client.sessions() as session, _scan_client() as http:
        assert (
            await extraction.extract_material(session, material_id, client=http)
        ) == "done"


# --- Reaching the student ------------------------------------------------------
#
# Everything above asserts the worker writes the right status. None of it
# asserts a student ever *sees* it, and for a while none of them did: the row
# went `done` in the database and the card in the app said "waiting to be read"
# until the install was deleted.
#
# `GET /sync` is cursor-based and the cursor is `materials.updated_at`, so a
# status the cursor cannot reach is a status that did not happen. These read
# through the endpoint rather than out of the database, which is the only way
# the difference shows up.


async def _cursor(client, headers) -> str:
    return (await client.get("/api/v1/sync", headers=headers)).json()["cursor"]


async def _pull(client, headers, since: str) -> dict:
    body = (await client.get(f"/api/v1/sync?since={since}", headers=headers)).json()
    return {row["id"]: row for row in body["materials"]}


async def test_a_finished_document_reaches_a_device_that_already_synced(client):
    """
    The regression test for the bug that made the whole feature invisible.

    `updated_at` was being written — `onupdate=func.now()` fires — but Postgres
    `now()` is the *transaction* timestamp, and this worker opens a transaction,
    downloads a file, parses it, and only then commits. The row landed stamped
    from before all of that, so a device polling in the meantime held a cursor
    ahead of it and never saw the row again.

    Reading the row back from the database passes either way. Only a pull with a
    cursor taken before extraction catches it.
    """
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    # The app has synced and holds a cursor.
    cursor = await _cursor(client, headers)

    async with (
        client.sessions() as session,
        _client_serving(_pdf([TRANSCRIPT])) as http,
    ):
        assert (
            await extraction.extract_material(session, material_id, client=http)
        ) == "done"

    pulled = await _pull(client, headers, cursor)

    assert str(material_id) in pulled, "the finished document never comes back"
    assert pulled[str(material_id)]["extraction_status"] == "done"


async def test_a_failure_and_its_reason_reach_the_device(client):
    """A reason the app never receives is a spinner that never stops."""
    headers, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    cursor = await _cursor(client, headers)

    async with client.sessions() as session, _client_serving(b"not a pdf") as http:
        await extraction.extract_material(session, material_id, client=http)

    row = (await _pull(client, headers, cursor))[str(material_id)]

    assert row["extraction_status"] == "failed"
    assert row["extraction_error"]


async def test_a_scan_reaches_the_device_with_its_page_count(client, ocr_configured):
    """
    One photo is one page, and the app prints it.

    Bundled with the visibility tests because `page_count` was always being set
    — it just travelled on the same row that never arrived.
    """
    headers, user_id = await sign_in(client)
    await _give(client, user_id, Tier.PRO)
    material_id = await _scan(client, user_id)

    cursor = await _cursor(client, headers)

    async with client.sessions() as session, _scan_client() as http:
        await extraction.extract_material(session, material_id, client=http)

    row = (await _pull(client, headers, cursor))[str(material_id)]

    assert row["extraction_status"] == "done"
    assert row["page_count"] == 1


async def test_every_transition_moves_the_cursor(client, ocr_configured):
    """
    Not only the happy one.

    A worker that stamps four transitions out of five produces a bug that only
    appears for one kind of failure, which is the hardest kind to find. This
    walks the row through each status and asserts the stamp moved every time.
    """
    from app.models.knowledge import Material as M
    from app.workers.extraction import set_status

    _, user_id = await sign_in(client)
    material_id = await _material(client, user_id)

    seen = []
    for status, error in (
        ("running", None),
        ("done", ""),
        ("failed", "That PDF is password protected."),
        ("skipped", "Scanning handwritten notes is a Synapse feature."),
        ("pending", None),
    ):
        async with client.sessions() as session:
            material = await session.get(M, material_id)
            set_status(material, status, error=error)
            await session.commit()
            seen.append(as_utc(material.updated_at))

    assert seen == sorted(seen), "a transition did not move updated_at forward"
    assert len(set(seen)) == len(seen), "two transitions share a timestamp"


async def test_scans_stuck_with_no_vision_key_are_reported(client, monkeypatch, capsys):
    """
    The silent failure, made audible.

    A photograph uploaded to a box with no vision key is never claimed, so no
    code path runs, nothing raises, and no line appears in any log. The card in
    the app says "waiting to be read" for ever and the server offers no
    explanation — which is the shape of problem that costs an afternoon.
    """
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "ocr_api_key", "")

    _, user_id = await sign_in(client)
    scan_id = await _scan(client, user_id)

    async with client.sessions() as session:
        # Nothing claims it — that is the bug being reported, not a failure.
        assert scan_id not in await extraction.claim_batch(session, 10)

        waiting = await extraction.report_unreadable_backlog(session)

    assert waiting == 1
    # Read off stdout rather than `caplog`: structlog renders straight there,
    # so the stdlib capture sees nothing even though the line is emitted.
    printed = capsys.readouterr().out
    assert "scans_waiting_for_a_vision_key" in printed
    assert "GOOGLE_API_KEY" in printed


async def test_nothing_is_reported_once_a_key_is_set(client, ocr_configured):
    """A warning that fires when everything is fine is a warning nobody reads."""
    _, user_id = await sign_in(client)
    await _scan(client, user_id)

    async with client.sessions() as session:
        assert await extraction.report_unreadable_backlog(session) == 0


def test_the_missing_vision_key_is_named_at_startup(monkeypatch):
    """
    Startup lists what will not work. OCR was absent from that list, which is
    part of why this failed quietly.
    """
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "ocr_api_key", "")

    reported = " ".join(settings.unavailable_features())

    assert "GOOGLE_API_KEY" in reported
    assert "pending" in reported
