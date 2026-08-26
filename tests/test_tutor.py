"""
The tutor.

Three things are worth pinning here, and they are the three that decide whether
a student can trust an answer:

* **Grounding.** A question the notes cover is answered from them and cites
  them. A question they do not cover says so, in those words, before answering.
* **Intent.** "Hello" and "what do you think about computer science" are not
  coursework, and must not be met with "I could not find that in your material".
* **Isolation.** Retrieval never crosses accounts. This is the one failure in
  the whole system that would be unforgivable.

No test here reaches the network. `_FakeProvider` stands in for DeepSeek and
records what it was asked, which is where the interesting assertions are — the
*prompt* is what makes an answer grounded, not the reply.
"""

import json
import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.config import settings
from app.models.course import Unit
from app.models.knowledge import Material, MaterialChunk
from app.services.ai import pipeline, prompts, providers, retrieval
from app.services.ai.providers import Message, Usage
from app.services.ai.sanitise import StreamCleaner
from tests.conftest import OTHER_PHONE, sign_in

NOTES = (
    "A deadlock occurs when two processes each hold a resource the other needs "
    "and neither can proceed. The four Coffman conditions are mutual exclusion, "
    "hold and wait, no preemption, and circular wait."
)


# --- A stand-in for the model -------------------------------------------------


class _FakeProvider:
    """
    Records what it was asked and replies with whatever it was told to.

    The classifier and the answer both go through this, so `verdict` decides the
    routing and `reply` decides the text.
    """

    def __init__(self, *, reply: str = "An answer.", verdict: str = "COURSEWORK") -> None:
        self.reply = reply
        self.verdict = verdict
        self.stream_calls: list[list[Message]] = []
        self.complete_calls: list[list[Message]] = []

    async def stream(self, messages, *, model, max_tokens, temperature) -> AsyncIterator[str]:
        self.stream_calls.append(messages)
        # Deliberately split mid-word: a provider chunks wherever it likes, and
        # anything that only works on whole words is broken.
        for i in range(0, len(self.reply), 7):
            yield self.reply[i : i + 7]

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.complete_calls.append(messages)
        return self.verdict, Usage(prompt_tokens=10, completion_tokens=1)

    @property
    def system_prompt(self) -> str:
        return self.stream_calls[-1][0].content

    @property
    def user_prompt(self) -> str:
        return self.stream_calls[-1][-1].content


@pytest.fixture
def fake_model(monkeypatch):
    """Makes DeepSeek 'configured' and swaps the transport for the fake."""
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    fake = _FakeProvider()
    monkeypatch.setattr(providers, "provider_for", lambda spec: fake)
    monkeypatch.setattr(pipeline.providers, "provider_for", lambda spec: fake)
    return fake


async def _give_notes(client, user_id, *, code="CS201", title="Week 4 notes", body=NOTES):
    """Files one document with one chunk under a unit, as extraction would."""
    async with client.sessions() as session:
        unit = Unit(id=uuid.uuid4(), user_id=user_id, code=code, title="Operating Systems")
        session.add(unit)
        await session.flush()

        material = Material(
            id=uuid.uuid4(),
            user_id=user_id,
            unit_id=unit.id,
            kind="pdf",
            title=title,
            extraction_status="done",
        )
        session.add(material)
        await session.flush()

        session.add(
            MaterialChunk(
                id=uuid.uuid4(),
                material_id=material.id,
                user_id=user_id,
                ordinal=0,
                page_number=4,
                content=body,
            )
        )
        await session.commit()
        return material.id


def _frames(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, payload) pairs."""
    out = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            out.append((name, data))
    return out


def _text(frames) -> str:
    return "".join(payload["text"] for name, payload in frames if name == "token")


# --- Retrieval ----------------------------------------------------------------


async def test_retrieval_finds_the_students_own_notes(client):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    async with client.sessions() as session:
        found = await retrieval.search(
            session, user_id=user_id, question="What is a deadlock?"
        )

    assert found
    assert "Coffman" in found[0].content
    assert found[0].page_number == 4
    assert retrieval.is_grounded(found)


async def test_retrieval_never_crosses_accounts(client):
    """
    The one failure that would be unforgivable.

    `material_chunks.user_id` is denormalised from the material precisely so
    this filter cannot be forgotten, and this is the test that says so.
    """
    _, mine = await sign_in(client)
    _, theirs = await sign_in(client, phone=OTHER_PHONE)
    await _give_notes(client, theirs)

    async with client.sessions() as session:
        found = await retrieval.search(
            session, user_id=mine, question="What is a deadlock?"
        )

    assert found == []


async def test_a_question_the_notes_do_not_cover_scores_low(client):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    async with client.sessions() as session:
        found = await retrieval.search(
            session, user_id=user_id, question="Who wrote Things Fall Apart?"
        )

    assert not retrieval.is_grounded(found)


async def test_a_question_of_only_stopwords_retrieves_nothing(client):
    """No terms means no search, rather than a match on everything."""
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    async with client.sessions() as session:
        assert await retrieval.search(session, user_id=user_id, question="what is it?") == []


# --- Routing ------------------------------------------------------------------


async def test_a_covered_question_is_answered_from_the_material(client, fake_model):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "Explain deadlock"}, headers=headers
    )

    assert response.status_code == 200
    frames = _frames(response.text)
    meta = next(payload for name, payload in frames if name == "meta")

    assert meta["mode"] == "grounded"
    assert meta["grounded"] is True
    assert meta["sources"], "a grounded answer must say what it was built from"
    assert meta["sources"][0]["page_number"] == 4

    # The passages have to actually reach the model, or "grounded" is a label
    # on a prompt that never saw the notes.
    assert "Coffman" in fake_model.user_prompt
    assert fake_model.system_prompt == prompts.GROUNDED

    assert prompts.NOT_IN_MATERIAL not in _text(frames)


async def test_an_uncovered_question_says_so_before_answering(client, fake_model):
    """
    The behaviour this whole pipeline exists for.

    The student is told the notes came up short *first* — not after a paragraph
    they have already started believing came from their own material.
    """
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    response = await client.post(
        "/api/v1/tutor/ask",
        json={"question": "Who wrote Things Fall Apart?"},
        headers=headers,
    )

    frames = _frames(response.text)
    meta = next(payload for name, payload in frames if name == "meta")
    answer = _text(frames)

    assert meta["mode"] == "general"
    assert meta["grounded"] is False
    assert meta["sources"] == []
    assert answer.startswith(prompts.NOT_IN_MATERIAL)
    assert fake_model.system_prompt == prompts.GENERAL


async def test_small_talk_is_not_told_the_notes_came_up_short(client, fake_model):
    """
    "Hello" must not be answered with "I could not find that in your material".

    A greeting is recognised without a model round trip, which is also why this
    passes with the classifier hard-wired to say COURSEWORK.
    """
    headers, _ = await sign_in(client)
    fake_model.verdict = "COURSEWORK"

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "hello"}, headers=headers
    )

    frames = _frames(response.text)
    meta = next(payload for name, payload in frames if name == "meta")

    assert meta["mode"] == "chat"
    assert prompts.NOT_IN_MATERIAL not in _text(frames)
    assert fake_model.system_prompt == prompts.CHAT
    assert not fake_model.complete_calls, "a bare greeting should not cost a classifier call"


async def test_an_opinion_question_is_chat_not_coursework(client, fake_model):
    """
    "What do you think about computer science" is coursework by every keyword
    test and small talk to any reader. This is why classification is a model
    call rather than a regex.
    """
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    fake_model.verdict = "CHAT"

    response = await client.post(
        "/api/v1/tutor/ask",
        json={"question": "What do you think about computer science?"},
        headers=headers,
    )

    frames = _frames(response.text)
    meta = next(payload for name, payload in frames if name == "meta")

    assert meta["mode"] == "chat"
    assert prompts.NOT_IN_MATERIAL not in _text(frames)


async def test_a_classifier_failure_falls_back_to_coursework(client, fake_model, monkeypatch):
    """
    The safe direction.

    Coursework answered conversationally loses its citations; small talk
    answered as coursework is merely a little stiff.
    """
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(fake_model, "complete", boom)

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "Explain deadlock"}, headers=headers
    )

    meta = next(p for n, p in _frames(response.text) if n == "meta")
    assert meta["mode"] == "grounded"


# --- Formatting ---------------------------------------------------------------


async def test_markdown_never_reaches_the_student(client, fake_model):
    """
    The app has no markdown renderer, so an asterisk on screen is a bug.

    The reply is chunked mid-marker on purpose — `**` routinely arrives split
    across two chunks, which is exactly where a naive cleaner leaks.
    """
    headers, _ = await sign_in(client)
    fake_model.reply = "**Key idea:**\n- First point\n- Second point\nSee *this* too."

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "hello"}, headers=headers
    )

    answer = _text(_frames(response.text))

    assert "*" not in answer
    assert not any(line.startswith("- ") for line in answer.splitlines())
    assert "Key idea:" in answer
    assert "First point" in answer


def test_cleaning_does_not_depend_on_how_the_stream_is_chunked():
    """
    The invariant that makes streaming safe.

    A marker split across a chunk boundary must clean identically to one that
    is not, or the same answer looks different depending on the provider's
    packet sizes.
    """
    text = "**Bold** and *italic* and\n- a bullet\nplus [a link](http://x.y) at the end."

    whole = StreamCleaner()
    expected = whole.feed(text) + whole.flush()

    for size in (1, 2, 3, 5, 13):
        cleaner = StreamCleaner()
        got = "".join(cleaner.feed(text[i : i + size]) for i in range(0, len(text), size))
        got += cleaner.flush()
        assert got == expected, f"chunk size {size} produced different output"

    assert "*" not in expected
    assert "http://x.y" not in expected


def test_ordinary_hyphens_survive():
    """
    "No dashes" means markdown bullets, not every hyphen in English.

    Stripping them all would turn "state-of-the-art" into "stateoftheart" and a
    temperature of -5 into 5.
    """
    cleaner = StreamCleaner()
    text = "A state-of-the-art result at -5 degrees, over the range 5-10.\n"
    out = cleaner.feed(text) + cleaner.flush()

    assert out.strip() == text.strip()


# --- The model catalogue ------------------------------------------------------


async def test_models_lists_the_unavailable_ones_too(client, monkeypatch):
    """
    The picker shows the whole line-up.

    A list that silently grows later is a worse experience than one that shows
    what is coming, and "no key" is a different problem from "no adapter".
    """
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    headers, _ = await sign_in(client)

    body = (await client.get("/api/v1/tutor/models", headers=headers)).json()
    by_id = {model["id"]: model for model in body["models"]}

    assert body["default"] == "deepseek-chat"
    assert by_id["deepseek-chat"]["available"] is True

    assert by_id["claude-sonnet-5"]["available"] is False
    assert by_id["claude-sonnet-5"]["implemented"] is False
    assert "adapter" in by_id["claude-sonnet-5"]["note"]

    assert by_id["gpt-4.1-mini"]["available"] is False
    assert by_id["gpt-4.1-mini"]["implemented"] is True
    assert "OPENAI_API_KEY" in by_id["gpt-4.1-mini"]["note"]


def test_an_unavailable_choice_falls_back_rather_than_failing(monkeypatch):
    """
    A student who picked Claude before the key existed still gets an answer.

    The response names the model that actually replied, so the substitution is
    visible rather than silent.
    """
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    assert providers.resolve("claude-sonnet-5").id == "deepseek-chat"
    assert providers.resolve("nonsense").id == "deepseek-chat"
    assert providers.resolve(None).id == "deepseek-chat"


def test_with_no_keys_at_all_the_tutor_refuses(monkeypatch):
    from app.core.errors import AppError

    for key in ("deepseek_api_key", "openai_api_key", "anthropic_api_key", "google_api_key"):
        monkeypatch.setattr(settings, key, "")

    with pytest.raises(AppError):
        providers.resolve(None)


async def test_asking_without_a_configured_provider_is_a_clean_error(client, monkeypatch):
    for key in ("deepseek_api_key", "openai_api_key", "anthropic_api_key", "google_api_key"):
        monkeypatch.setattr(settings, key, "")
    headers, _ = await sign_in(client)

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "Explain deadlock"}, headers=headers
    )

    assert response.status_code == 400
    assert "not configured" in response.json()["message"]


# --- Quota and persistence ----------------------------------------------------


async def test_asking_needs_a_signed_in_student(client):
    response = await client.post("/api/v1/tutor/ask", json={"question": "hi"})
    assert response.status_code == 401


async def test_the_question_is_charged_before_the_answer_is_generated(client, fake_model):
    """
    Charging afterwards is the cheapest possible way past a daily limit: ask,
    disconnect halfway, repeat.
    """
    headers, user_id = await sign_in(client)

    await client.post("/api/v1/tutor/ask", json={"question": "hello"}, headers=headers)

    async with client.sessions() as session:
        from app.services.quota import current_usage

        assert await current_usage(session, user_id, "ai_queries") == 1


async def test_both_turns_are_saved_with_the_model_that_answered(client, fake_model):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    fake_model.reply = "Deadlock is a standstill."

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "Explain deadlock"}, headers=headers
    )
    chat_id = next(p for n, p in _frames(response.text) if n == "meta")["chat_id"]

    async with client.sessions() as session:
        from sqlalchemy import select

        from app.models.tutor import Message as MessageRow

        rows = (
            await session.scalars(
                select(MessageRow)
                .where(MessageRow.chat_id == uuid.UUID(chat_id))
                .order_by(MessageRow.created_at)
            )
        ).all()

    roles = [row.role for row in rows]
    assert "student" in roles and "tutor" in roles

    answer = next(row for row in rows if row.role == "tutor")
    assert answer.content == "Deadlock is a standstill."
    assert answer.model == "deepseek-chat"
    assert answer.sources, "a grounded answer records what it cited"


async def test_a_follow_up_stays_in_the_same_chat(client, fake_model):
    headers, user_id = await sign_in(client)
    chat_id = str(uuid.uuid4())

    for question in ("What is a deadlock?", "And how do you prevent one?"):
        await client.post(
            "/api/v1/tutor/ask",
            json={"question": question, "chat_id": chat_id},
            headers=headers,
        )

    # The second call must have been given the first exchange as context.
    assert any(
        "What is a deadlock?" in message.content
        for message in fake_model.stream_calls[-1]
    )


async def test_a_chat_id_belonging_to_someone_else_is_not_readable(client, fake_model):
    """A chat id is client-minted, so guessing one must reveal nothing."""
    mine_headers, _ = await sign_in(client)
    theirs_headers, _ = await sign_in(client, phone=OTHER_PHONE)

    chat_id = str(uuid.uuid4())
    await client.post(
        "/api/v1/tutor/ask",
        json={"question": "My private question about deadlock", "chat_id": chat_id},
        headers=theirs_headers,
    )

    fake_model.stream_calls.clear()
    await client.post(
        "/api/v1/tutor/ask",
        json={"question": "hello", "chat_id": chat_id},
        headers=mine_headers,
    )

    assert not any(
        "My private question" in message.content
        for message in fake_model.stream_calls[-1]
    )


# --- Streaming shape ----------------------------------------------------------


async def test_the_stream_opens_with_meta_and_closes_with_done(client, fake_model):
    """
    `meta` first so the app can draw the citation header while the answer is
    still arriving; `done` last so a client can keep the whole text without
    accumulating tokens itself.
    """
    headers, _ = await sign_in(client)
    fake_model.reply = "A short answer."

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "hello"}, headers=headers
    )

    assert response.headers["content-type"].startswith("text/event-stream")
    # nginx buffers proxied responses by default, which would hold the whole
    # answer back and deliver it in one lump.
    assert response.headers["x-accel-buffering"] == "no"

    frames = _frames(response.text)
    assert frames[0][0] == "meta"
    assert frames[-1][0] == "done"
    assert frames[-1][1]["text"] == "A short answer."
    assert _text(frames) == "A short answer."


async def test_a_provider_failure_arrives_as_an_error_frame(client, fake_model, monkeypatch):
    """
    The headers went out with `meta`, so there is no status code left to change.
    The failure has to travel inside the stream.
    """
    from app.core.errors import AppError

    headers, _ = await sign_in(client)

    async def broken(*_args, **_kwargs):
        raise AppError("The tutor is busy right now. Try again in a moment.")
        yield ""

    monkeypatch.setattr(fake_model, "stream", broken)

    response = await client.post(
        "/api/v1/tutor/ask", json={"question": "hello"}, headers=headers
    )

    assert response.status_code == 200
    frames = _frames(response.text)
    names = [name for name, _ in frames]

    assert "error" in names
    assert "busy" in frames[-1][1]["message"]
