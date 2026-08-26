"""
Quiz building.

The prompt is the easy half. The hard half is not trusting what comes back: a
model asked for JSON returns JSON most of the time and prose wrapped around JSON
the rest of it, and a malformed question is worse than a missing one — the
student answers, is told they are wrong, and has no way to tell whether the
question was broken or their understanding was.

So most of what is pinned here is what happens to bad output.
"""

import json
import uuid

import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.models.course import Unit
from app.models.knowledge import Material, MaterialChunk
from app.services.ai import providers
from app.services.ai import quiz as quiz_service
from app.services.ai.providers import Usage
from app.services.plans import Tier
from tests.conftest import sign_in

NOTES = (
    "A deadlock occurs when two processes each hold a resource the other needs. "
    "The four Coffman conditions are mutual exclusion, hold and wait, no "
    "preemption, and circular wait. Prevention removes one of the four."
)


def _question(prompt="What is a deadlock?", answer=0, options=None):
    return {
        "prompt": prompt,
        "options": options or ["A standstill", "A crash", "A race", "A leak"],
        "answer": answer,
        "explanation": "Neither process can proceed.",
    }


class _FakeProvider:
    """Returns whatever raw text the test wants the model to have produced."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(messages)
        return self.text, Usage(prompt_tokens=100, completion_tokens=200)

    async def stream(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("a quiz is not streamed")

    @property
    def user_prompt(self) -> str:
        return self.calls[-1][-1].content


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    holder = {}

    def install(text):
        provider = _FakeProvider(text)
        monkeypatch.setattr(providers, "provider_for", lambda spec: provider)
        monkeypatch.setattr(quiz_service.providers, "provider_for", lambda spec: provider)
        holder["provider"] = provider
        return provider

    install(json.dumps({"questions": [_question()]}))
    return type("Fake", (), {"install": staticmethod(install), "holder": holder})


async def _give_notes(client, user_id, *, code="CS201"):
    async with client.sessions() as session:
        unit = Unit(id=uuid.uuid4(), user_id=user_id, code=code, title="Operating Systems")
        session.add(unit)
        await session.flush()
        material = Material(
            id=uuid.uuid4(),
            user_id=user_id,
            unit_id=unit.id,
            kind="pdf",
            title="Week 4 notes",
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
                content=NOTES,
            )
        )
        await session.commit()


# --- Parsing whatever the model actually sent ---------------------------------


def test_a_code_fence_around_the_json_is_survivable():
    """Models wrap JSON in a fence constantly, despite being told not to."""
    body = json.dumps({"questions": [_question()]})
    assert quiz_service._extract_json(f"```json\n{body}\n```") is not None


def test_a_sentence_of_preamble_is_survivable():
    body = json.dumps({"questions": [_question()]})
    parsed = quiz_service._extract_json(f"Here is your quiz:\n\n{body}")
    assert parsed and len(parsed["questions"]) == 1


def test_a_trailing_comma_is_repaired():
    """`json` refuses it; models produce it constantly."""
    assert quiz_service._extract_json('{"questions": [{"a": 1},]}') is not None


def test_unparseable_output_is_nothing_rather_than_an_exception():
    assert quiz_service._extract_json("I am afraid I cannot do that.") is None
    assert quiz_service._extract_json("") is None


# --- Validation ---------------------------------------------------------------


def test_a_good_question_survives():
    question = quiz_service._valid(_question())
    assert question and question.answer == 0 and len(question.options) == 4


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({**_question(), "options": ["a", "b", "c"]}, "three options"),
        ({**_question(), "answer": 7}, "answer out of range"),
        ({**_question(), "answer": None}, "no answer"),
        ({**_question(), "answer": "second"}, "answer not an index"),
        ({**_question(), "prompt": ""}, "no prompt"),
        ({**_question(), "options": ["a", "a", "b", "c"]}, "duplicate options"),
        ({**_question(), "options": ["a", "", "b", "c"]}, "an empty option"),
    ],
)
def test_a_broken_question_is_dropped_not_repaired(bad, why):
    """
    Padding a three-option question or picking an answer at random would
    silently teach the student something false. A shorter quiz is honest.
    """
    assert quiz_service._valid(bad) is None, why


def test_markdown_is_stripped_from_questions_and_options():
    """The app renders these as plain text, same as an answer."""
    question = quiz_service._valid(
        {
            "prompt": "**What** is a deadlock?",
            "options": ["*A standstill*", "A crash", "A race", "A leak"],
            "answer": 0,
            "explanation": "Neither can __proceed__.",
        }
    )

    assert "*" not in question.prompt
    assert "*" not in question.options[0]
    assert "_" not in question.explanation


# --- Building -----------------------------------------------------------------


async def test_a_quiz_from_the_students_own_material_says_so(client, fake):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    provider = fake.install(json.dumps({"questions": [_question(), _question("And another?")]}))

    async with client.sessions() as session:
        built = await quiz_service.build(
            session, user_id=user_id, unit_code="CS201", topic="deadlock", count=2
        )

    assert built.grounded is True
    assert built.note == quiz_service.GROUNDED_NOTE
    assert len(built.questions) == 2
    assert built.questions[0].source, "a grounded question records where it came from"
    # The passages must actually reach the model, or "grounded" is a label on a
    # prompt that never saw the notes.
    assert "Coffman" in provider.user_prompt


async def test_a_topic_the_material_misses_falls_back_and_says_so(client, fake):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    fake.install(json.dumps({"questions": [_question()]}))

    async with client.sessions() as session:
        built = await quiz_service.build(
            session, user_id=user_id, unit_code=None, topic="Nigerian literature", count=1
        )

    assert built.grounded is False
    assert built.note == quiz_service.GENERAL_NOTE


async def test_a_short_quiz_from_the_material_is_flagged_as_partial(client, fake):
    """
    Asked for five, the model gave two. The student is told the material was
    thin rather than left wondering why the quiz is short.
    """
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    fake.install(json.dumps({"questions": [_question()]}))

    async with client.sessions() as session:
        built = await quiz_service.build(
            session, user_id=user_id, unit_code="CS201", topic="deadlock", count=5
        )

    assert built.note == quiz_service.PARTIAL_NOTE


async def test_broken_questions_are_dropped_and_the_rest_kept(client, fake):
    headers, user_id = await sign_in(client)
    fake.install(
        json.dumps(
            {
                "questions": [
                    _question("Good one"),
                    {**_question("Bad one"), "options": ["only", "two"]},
                    _question("Another good one"),
                ]
            }
        )
    )

    async with client.sessions() as session:
        built = await quiz_service.build(
            session, user_id=user_id, unit_code=None, topic="deadlock", count=3
        )

    prompts = [question.prompt for question in built.questions]
    assert prompts == ["Good one", "Another good one"]


async def test_a_quiz_with_nothing_usable_is_an_error_not_an_empty_quiz(client, fake):
    """An empty list would leave the app rendering a quiz with no questions."""
    headers, user_id = await sign_in(client)
    fake.install("the model refused")

    with pytest.raises(AppError):
        async with client.sessions() as session:
            await quiz_service.build(
                session, user_id=user_id, unit_code=None, topic="deadlock", count=3
            )


async def test_a_quiz_over_nothing_is_refused(client, fake):
    headers, user_id = await sign_in(client)

    with pytest.raises(AppError) as caught:
        async with client.sessions() as session:
            await quiz_service.build(
                session, user_id=user_id, unit_code=None, topic=None, count=3
            )

    assert "what to quiz you on" in str(caught.value)


# --- Limits -------------------------------------------------------------------


def test_the_count_is_clamped_to_the_plan_not_the_request():
    """A client asking for fifty gets its plan's ceiling."""
    assert quiz_service.clamp(50, 10) == 10
    assert quiz_service.clamp(3, 10) == 3
    assert quiz_service.clamp(None, 10) == 5
    assert quiz_service.clamp(0, 10) == 5


# --- The endpoint -------------------------------------------------------------


async def test_the_endpoint_returns_a_rendered_quiz(client, fake):
    headers, user_id = await sign_in(client)
    await _give_notes(client, user_id)
    fake.install(json.dumps({"questions": [_question(), _question("Second?")]}))

    response = await client.post(
        "/api/v1/tutor/quiz",
        json={"unit_code": "CS201", "topic": "deadlock", "count": 2},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["questions"]) == 2
    assert body["grounded"] is True
    assert body["note"]
    assert body["model"] == "deepseek-chat"
    assert 0 <= body["questions"][0]["answer"] <= 3


async def test_a_quiz_needs_a_signed_in_student(client):
    response = await client.post("/api/v1/tutor/quiz", json={"topic": "deadlock"})
    assert response.status_code == 401


async def test_a_lapsed_plan_cannot_build_quizzes(client, fake):
    """
    `expired` allows zero, and the refusal is a 402 — "not included in what you
    pay for", not "too fast".
    """
    headers, user_id = await sign_in(client)

    async with client.sessions() as session:
        from app.services.billing import activate

        await activate(session, user_id=user_id, tier=Tier.EXPIRED, verified=False)
        await session.commit()

    response = await client.post(
        "/api/v1/tutor/quiz", json={"topic": "deadlock"}, headers=headers
    )

    assert response.status_code == 402


async def test_the_quiz_allowance_is_spent(client, fake):
    headers, user_id = await sign_in(client)

    await client.post("/api/v1/tutor/quiz", json={"topic": "deadlock"}, headers=headers)

    async with client.sessions() as session:
        from app.services.quota import current_usage

        # The trial counts quizzes for life, not per week.
        assert await current_usage(session, user_id, "quizzes_lifetime") == 1


async def test_a_failed_build_does_not_spend_the_allowance(client, fake):
    """
    Charged after the build, unlike an answer.

    A weekly quiz allowance is small enough that spending one on a provider
    outage is a real loss to the student, and the race it opens is one quiz
    wide.
    """
    headers, user_id = await sign_in(client)
    fake.install("the model refused")

    response = await client.post(
        "/api/v1/tutor/quiz", json={"topic": "deadlock"}, headers=headers
    )
    assert response.status_code == 400

    async with client.sessions() as session:
        from app.services.quota import current_usage

        assert await current_usage(session, user_id, "quizzes_lifetime") == 0
