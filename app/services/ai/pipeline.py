"""
Deciding what kind of answer a question deserves, then giving it.

The shape is a small graph, and it is written as one deliberately — nodes that
take a state and return a state, a router that picks an edge, and a fan-out
where two nodes run at once:

                    ┌── classify ──┐
    question ───────┤              ├──→ route ──→ chat | grounded | general
                    └── look up ───┘

`look up` is the database half: which unit is open and what is filed under it,
then the passages that match the question. Both halves reach the prompt -- the
context in every mode, because "which unit am I in" and "what is in my pdf" are
questions the tutor used to answer with a flat denial while the answer sat in
the request it was handed.

`classify` and `look up` do not depend on each other, so they run concurrently.
That matters: classification is a model round trip and retrieval is a database
query, and running them in series would add the slower of the two to every
single question for no reason.

This is the shape LangGraph exists to express, and it was the plan. It is hand
rolled because `langgraph` cannot be imported on the development machine — it
pulls `xxhash`, whose compiled extension this Windows box's Application Control
policy blocks, and so does `langchain-openai` via `tiktoken`. The node/router
split is kept faithful so that swapping LangGraph in later is a rewrite of this
file and nothing else.

Why classify with a model at all, rather than keywords: "what do you think about
computer science" is a coursework question by every keyword test and an opinion
question to any reader. Getting that wrong means answering small talk with "I
could not find this in your material", which is the exact failure this whole
pipeline exists to avoid.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.ai import context as context_service
from app.services.ai import prompts, providers, retrieval
from app.services.ai.context import StudentContext
from app.services.ai.providers import Message, ModelSpec, Usage
from app.services.ai.retrieval import Passage
from app.services.ai.sanitise import StreamCleaner

log = structlog.get_logger()

#: Long enough to see the question, short enough to be cheap. The classifier
#: answers in one word.
_CLASSIFIER_MAX_TOKENS = 4


@dataclass
class TutorState:
    """What the graph carries between nodes."""

    question: str
    user_id: uuid.UUID
    unit_code: str | None = None
    history: list[Message] = field(default_factory=list)

    intent: str = "COURSEWORK"
    passages: list[Passage] = field(default_factory=list)
    #: The unit that is open and what is filed under it. Not what the material
    #: says -- what there is.
    context: StudentContext = field(default_factory=lambda: context_service.EMPTY)

    #: chat | grounded | general — set by `route`.
    mode: str = "general"
    #: Prepended to the answer when the material came up short.
    preamble: str = ""


@dataclass
class Plan:
    """The graph's output: everything the streaming step needs."""

    mode: str
    messages: list[Message]
    passages: list[Passage]
    preamble: str
    spec: ModelSpec
    context: StudentContext = field(default_factory=lambda: context_service.EMPTY)


# --- Nodes -------------------------------------------------------------------


async def classify(state: TutorState, spec: ModelSpec) -> str:
    """
    COURSEWORK or CHAT.

    Falls back to COURSEWORK on any failure. That is the safe direction: a
    coursework question answered conversationally loses its citations, while
    small talk answered as coursework is merely slightly stiff.
    """
    # Not worth a round trip. A bare greeting is unambiguous and this is the
    # most common message in the app.
    stripped = state.question.strip().lower().rstrip("!?. ")
    if stripped in {"hi", "hey", "hello", "yo", "thanks", "thank you", "ok", "okay"}:
        return "CHAT"

    provider = providers.provider_for(spec)

    try:
        text, _ = await provider.complete(
            [
                Message(role="system", content=prompts.CLASSIFIER),
                Message(role="user", content=state.question),
            ],
            model=spec.id,
            max_tokens=_CLASSIFIER_MAX_TOKENS,
            temperature=0.0,
        )
    except Exception as error:  # noqa: BLE001 — never let this sink a question
        log.warning("tutor_classify_failed", error=str(error))
        return "COURSEWORK"

    return "CHAT" if "CHAT" in text.strip().upper() else "COURSEWORK"


async def retrieve(state: TutorState, session: AsyncSession) -> list[Passage]:
    """The student's own material, ranked. Never another student's."""
    try:
        return await retrieval.search(
            session,
            user_id=state.user_id,
            question=state.question,
            unit_code=state.unit_code,
        )
    except Exception as error:  # noqa: BLE001
        # A retrieval failure should degrade to answering from general
        # knowledge, not to failing the request. The student gets an answer and
        # an honest note that their material was not consulted.
        log.warning("tutor_retrieval_failed", error=str(error))
        return []


async def look_up(state: TutorState, session: AsyncSession) -> None:
    """
    Everything the database knows: the unit that is open, and the passages.

    One node rather than two because they share a session, and an AsyncSession
    is not safe to use from two coroutines at once — gathering them would
    interleave two statements on one connection. They are cheap and sequential
    here; the fan-out that matters is against the classifier's round trip.
    """
    state.context = await context_service.load(
        session, user_id=state.user_id, unit_code=state.unit_code
    )
    state.passages = await retrieve(state, session)


#: Words that make a question about the documents rather than about a subject.
#: "What is the pdf about" has no content word to search for — every term in it
#: names the container — so keyword retrieval can only ever come back empty.
_ABOUT_MATERIAL = {
    "pdf", "pdfs", "notes", "note", "material", "materials", "document",
    "documents", "file", "files", "slide", "slides", "deck", "upload",
    "uploaded", "uploads", "book", "textbook", "handout", "attachment",
}


def asks_about_material(question: str) -> bool:
    """Whether the student is asking about their own documents as objects."""
    return bool(set(retrieval.keywords(question)) & _ABOUT_MATERIAL)


def route(state: TutorState) -> str:
    """
    Which kind of answer this is.

    Chat wins outright — there is nothing to look up, so retrieval results are
    irrelevant even when they exist. Otherwise the retrieval score decides, and
    that single comparison is the whole "I could not find this in your
    material" behaviour.
    """
    if state.intent == "CHAT":
        return "chat"
    return "grounded" if retrieval.is_grounded(state.passages) else "general"


def compose(state: TutorState) -> list[Message]:
    """The prompt for the chosen mode."""
    messages = [
        Message(role="system", content=prompts.system_for(state.mode, state.context))
    ]

    # Prior turns, so follow-ups work. Trimmed by the caller — this just places
    # them between the system prompt and the new question.
    messages.extend(state.history)

    if state.mode == "grounded":
        messages.append(
            Message(
                role="user",
                content=(
                    f"{prompts.build_passages_block(state.passages)}\n\n"
                    f"Question: {state.question}"
                ),
            )
        )
    else:
        messages.append(Message(role="user", content=state.question))

    return messages


# --- The graph ---------------------------------------------------------------


async def plan(
    session: AsyncSession,
    *,
    question: str,
    user_id: uuid.UUID,
    unit_code: str | None = None,
    history: list[Message] | None = None,
    model_id: str | None = None,
) -> Plan:
    """
    Run everything up to the point of generating, and return the decision.

    Generation is left to the caller so the streaming path stays a plain async
    generator — easy to test, and easy to wrap in SSE without the graph having
    to know what a response looks like.
    """
    spec = providers.resolve(model_id)
    state = TutorState(
        question=question,
        user_id=user_id,
        unit_code=unit_code,
        history=history or [],
    )

    # The fan-out. Neither needs the other's result.
    state.intent, _ = await asyncio.gather(
        classify(state, spec),
        look_up(state, session),
    )

    state.mode = route(state)

    if (
        state.mode == "general"
        and state.intent == "COURSEWORK"
        and state.context.has_material
        and asks_about_material(state.question)
    ):
        # "What is the pdf about". Nothing matched because there was nothing to
        # match — the question is about the document, not about anything in it.
        # Answering that from general knowledge and prefixing "I could not find
        # this in your material" is how the tutor ended up telling a student it
        # could not see a file that was sitting in the unit on screen. Hand it
        # the front of each document instead and let it answer properly.
        lead = await retrieval.lead_passages(
            session, user_id=state.user_id, unit_code=state.unit_code
        )
        if lead:
            state.passages = lead
            state.mode = "grounded"

    if state.mode == "general" and state.intent == "COURSEWORK":
        # Only when the student could reasonably have expected their notes to
        # cover it. Saying "I could not find this in your material" in reply to
        # "hello" would be absurd, and `route` has already separated those.
        state.preamble = prompts.NOT_IN_MATERIAL

    log.info(
        "tutor_plan",
        mode=state.mode,
        intent=state.intent,
        passages=len(state.passages),
        top_score=round(state.passages[0].score, 4) if state.passages else 0.0,
        model=spec.id,
    )

    return Plan(
        mode=state.mode,
        messages=compose(state),
        passages=state.passages if state.mode == "grounded" else [],
        preamble=state.preamble,
        spec=spec,
        context=state.context,
    )


async def generate(plan_: Plan) -> AsyncIterator[str]:
    """
    The answer, cleaned, as it arrives.

    The preamble is yielded before the model is even called, so a student
    learns immediately that this one is not from their notes rather than
    reading a paragraph first and finding out afterwards.
    """
    if plan_.preamble:
        yield plan_.preamble

    provider = providers.provider_for(plan_.spec)
    cleaner = StreamCleaner()

    async for chunk in provider.stream(
        plan_.messages,
        model=plan_.spec.id,
        max_tokens=settings.ai_max_output_tokens,
        temperature=settings.ai_temperature,
    ):
        cleaned = cleaner.feed(chunk)
        if cleaned:
            yield cleaned

    tail = cleaner.flush()
    if tail:
        yield tail


def estimate_usage(prompt_text: str, answer: str) -> Usage:
    """
    A token estimate for the streaming path.

    The streamed response does carry a usage record, but only in its final
    event, and threading that back out of a generator that has already been
    consumed by an SSE writer costs more than the number is worth. Four
    characters to a token is the usual English approximation and is close
    enough for a cost dashboard, which is the only thing reading it.
    """
    return Usage(
        prompt_tokens=max(1, len(prompt_text) // 4),
        completion_tokens=max(1, len(answer) // 4),
    )
