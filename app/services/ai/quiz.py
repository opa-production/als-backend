"""
Building a quiz from a student's own material.

Not streamed, unlike an answer. A quiz is a structure the app renders as cards —
there is nothing to show until the last question is parsed, so streaming would
buy a progress bar and cost the ability to validate before returning.

The hard part is not the prompt, it is trusting the result. A model asked for
JSON returns JSON most of the time and prose wrapped around JSON the rest of it,
and a quiz with four options and no correct answer is worse than no quiz: the
student answers, is told they are wrong, and has no way to know the question was
broken rather than their understanding.

So everything that comes back is parsed defensively and validated question by
question, and anything malformed is dropped rather than shown.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.services.ai import prompts, providers, retrieval
from app.services.ai.providers import Message
from app.services.ai.sanitise import clean_line

log = structlog.get_logger()

#: More passages than an answer uses. A quiz should range over a topic rather
#: than drill into the one paragraph that matched best.
QUIZ_TOP_K = 10

#: Generous — a ten-question quiz with explanations is a lot of tokens, and a
#: quiz truncated mid-JSON is a quiz that parses to nothing.
QUIZ_MAX_TOKENS = 2200


@dataclass
class Question:
    prompt: str
    options: list[str]
    #: Index into `options`.
    answer: int
    explanation: str = ""
    #: Where it came from, when it came from the student's own material.
    source: str = ""


@dataclass
class Quiz:
    questions: list[Question] = field(default_factory=list)
    grounded: bool = False
    model: str = ""
    #: Non-empty when the material could not supply the whole quiz.
    note: str = ""


SYSTEM = f"""
You write revision quizzes for university students.

Return only JSON. No prose before or after it, no code fence, no explanation of
what you have returned. The JSON must be an object with a single key
"questions", whose value is an array. Each entry has exactly these keys:

  "prompt"      the question, as one sentence
  "options"     an array of exactly four distinct strings
  "answer"      the index (0, 1, 2 or 3) of the correct option
  "explanation" one sentence saying why that option is right

Rules that matter more than style:

Exactly one option is correct, and it must be unambiguously correct. Never write
"all of the above" or "none of the above". The wrong options must be plausible
to somebody who half-knows the topic — an obviously silly option teaches
nothing and makes the question free.

Vary which index is correct across the quiz. Do not put the answer at the same
position every time.

{prompts.FORMATTING}
""".strip()


GROUNDED_NOTE = "These questions come from your own material."
PARTIAL_NOTE = (
    "Your material did not cover enough for a full quiz, so some of these "
    "questions come from general knowledge."
)
GENERAL_NOTE = (
    "I could not find enough in your material for this, so these questions "
    "come from general knowledge."
)


def _extract_json(text: str) -> dict | None:
    """
    The JSON object out of whatever the model actually sent.

    Three failure shapes are common enough to handle rather than reject: a code
    fence around the object, a sentence of preamble before it, and a trailing
    comma. Each is trivially recoverable and each would otherwise throw away a
    perfectly good quiz.
    """
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None

    candidate = text[start : end + 1]

    try:
        return json.loads(candidate)
    except ValueError:
        pass

    # A trailing comma before a closing brace or bracket. Models do this
    # constantly and `json` refuses it.
    repaired = re.sub(r",(\s*[}\]])", r"\1", candidate)
    try:
        return json.loads(repaired)
    except ValueError:
        log.warning("quiz_unparseable", head=text[:200])
        return None


def _valid(raw: dict) -> Question | None:
    """
    One question, or nothing.

    Dropped rather than repaired. A question with three options could be padded
    and one with no correct answer could have one picked at random, but both
    would be silently teaching the student something false — and a shorter quiz
    is an honest outcome.
    """
    prompt = clean_line(str(raw.get("prompt") or "").strip())
    options = raw.get("options")
    answer = raw.get("answer")

    if not prompt or not isinstance(options, list) or len(options) != 4:
        return None

    cleaned = [clean_line(str(option).strip()) for option in options]
    if any(not option for option in cleaned):
        return None

    # Duplicate options make two answers correct, or make the right one
    # obvious by elimination.
    if len({option.lower() for option in cleaned}) != 4:
        return None

    try:
        index = int(answer)
    except (TypeError, ValueError):
        return None

    if not 0 <= index <= 3:
        return None

    return Question(
        prompt=prompt,
        options=cleaned,
        answer=index,
        explanation=clean_line(str(raw.get("explanation") or "").strip()),
    )


async def build(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unit_code: str | None,
    topic: str | None,
    count: int,
    model_id: str | None = None,
) -> Quiz:
    """
    A quiz, from the student's material where it can be and from general
    knowledge where it cannot.

    The same honesty as an answer: if the material does not cover the topic the
    quiz still gets built, and the student is told where the questions came
    from rather than being left to assume they came from their notes.
    """
    spec = providers.resolve(model_id)
    provider = providers.provider_for(spec)

    # A quiz with no topic is a quiz over everything the unit contains, so the
    # unit code is the query when nothing else is given.
    query = topic or unit_code or ""
    passages = (
        await retrieval.search(
            session,
            user_id=user_id,
            question=query,
            unit_code=unit_code,
            limit=QUIZ_TOP_K,
        )
        if query
        else []
    )

    grounded = retrieval.is_grounded(passages)

    if grounded:
        # Written out rather than chained. `topic or X if unit_code else Y`
        # binds as `(topic or X) if unit_code else Y`, which silently drops the
        # topic whenever no unit was given — the exact case a student asking
        # "quiz me on recursion" hits.
        if topic:
            subject = topic
        elif unit_code:
            subject = f"the material below from {unit_code}"
        else:
            subject = "the material below"

        user_content = (
            f"{prompts.build_passages_block(passages)}\n\n"
            f"Write {count} multiple-choice questions about {subject}. "
            "Every question must be answerable from the passages above."
        )
    else:
        subject = topic or unit_code
        if not subject:
            raise AppError(
                "Tell me what to quiz you on — a topic, or a unit you have notes for."
            )
        user_content = (
            f"Write {count} multiple-choice questions about {subject}, "
            "at university level."
        )

    text, _usage = await provider.complete(
        [Message(role="system", content=SYSTEM), Message(role="user", content=user_content)],
        model=spec.id,
        max_tokens=QUIZ_MAX_TOKENS,
        temperature=0.7,  # Higher than an answer: a quiz should vary between runs.
    )

    payload = _extract_json(text)
    raw_questions = (payload or {}).get("questions") or []

    questions: list[Question] = []
    for raw in raw_questions[: count * 2]:
        if not isinstance(raw, dict):
            continue
        question = _valid(raw)
        if question is not None:
            if grounded and passages:
                question.source = passages[0].citation()
            questions.append(question)
        if len(questions) >= count:
            break

    if not questions:
        # Better than returning an empty quiz the app has to interpret.
        raise AppError("The quiz could not be built just now. Try again in a moment.")

    dropped = len(raw_questions) - len(questions)
    if dropped > 0:
        log.info("quiz_questions_dropped", dropped=dropped, kept=len(questions))

    if grounded:
        note = GROUNDED_NOTE if len(questions) >= count else PARTIAL_NOTE
    else:
        note = GENERAL_NOTE

    return Quiz(questions=questions, grounded=grounded, model=spec.id, note=note)


def max_questions_for(entitlement) -> int:
    """
    What the plan allows in one quiz.

    Read from the plan table rather than taken from the request: a client that
    asks for fifty questions gets its plan's ceiling, not fifty.
    """
    limit = entitlement.limits.quiz_max_questions
    return max(1, min(limit, 20)) if limit else 0


def clamp(requested: int | None, ceiling: int) -> int:
    if not requested or requested < 1:
        return min(5, ceiling)
    return min(requested, ceiling)


#: Re-exported so the route does not have to reach into settings for it.
DEFAULT_MODEL = settings.ai_default_model
