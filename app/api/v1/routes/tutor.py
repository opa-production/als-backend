"""
The tutor.

Two shapes of endpoint, because they answer two different questions:

* ``GET /tutor/models`` — what a student may pick from. Lists every model the
  product means to offer, including the ones that do not work yet, so the app
  can show the full line-up with the rest greyed out.
* ``POST /tutor/ask`` — the answer, streamed.

Streaming is server-sent events. The alternative was to buffer and return JSON,
which is simpler and wrong for this: a grounded answer takes several seconds to
generate, and a student staring at a spinner for six seconds assumes it has
hung. SSE also degrades honestly — a connection that drops mid-answer leaves the
student with the part that arrived rather than nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.core.errors import AppError
from app.models.tutor import Chat
from app.models.tutor import Message as MessageRow
from app.services.ai import pipeline, providers
from app.services.ai import quiz as quiz_service
from app.services.ai.providers import Message
from app.services.quota import (
    check_ai_query,
    check_quiz,
    get_entitlement,
    quiz_metric,
    record_ai_query,
    record_usage,
)

log = structlog.get_logger()
router = APIRouter()

#: How many prior turns go into the prompt. Enough for "and what about the
#: second one" to work; few enough that a long conversation does not quietly
#: become the most expensive thing in the product.
_HISTORY_TURNS = 6


# --- Shapes -------------------------------------------------------------------


class ModelOut(BaseModel):
    id: str
    provider: str
    label: str
    description: str
    available: bool
    #: False when the adapter itself is unwritten, as opposed to a key merely
    #: being absent. Different problems, different fixes.
    implemented: bool
    note: str
    tags: list[str]


class ModelsOut(BaseModel):
    models: list[ModelOut]
    #: What `POST /ask` will use when the request names nothing.
    default: str | None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    #: Keeps a conversation together. The client mints it, like every other id.
    chat_id: uuid.UUID | None = None
    #: Narrows retrieval to one unit, e.g. "CS201".
    unit_code: str | None = Field(default=None, max_length=16)
    #: Omit to take the server's default. An unavailable choice falls back
    #: rather than failing, and the stream says which model actually answered.
    model: str | None = None
    #: The ids the device used for this turn, so the two sides describe the
    #: same two rows rather than each inventing their own.
    #:
    #: Without these the server saved the question and the answer under ids the
    #: device had never seen, the device saved them under ids the server had
    #: never seen, and the next sync pulled the server's pair down as *extra*
    #: messages -- so every answer appeared twice, as though the question had
    #: been asked twice.
    #:
    #: Optional, so an older build still works: the server mints them when they
    #: are absent and reports both in the `meta` frame, which lets a client
    #: adopt them instead of minting its own.
    student_message_id: uuid.UUID | None = None
    answer_message_id: uuid.UUID | None = None


class SourceOut(BaseModel):
    material_id: uuid.UUID
    title: str
    unit_code: str
    page_number: int | None


# --- Models -------------------------------------------------------------------


@router.get("/models", response_model=ModelsOut, summary="What the tutor can use")
async def list_models(user: CurrentUser) -> ModelsOut:
    """
    Every model, available or not.

    Unavailable ones are listed rather than hidden on purpose: the app's picker
    is where a student learns that Claude and Gemini are coming, and a list that
    silently grows later is a worse experience than one that shows the shape up
    front.
    """
    catalogue = providers.catalogue()
    default = next((spec.id for spec in catalogue if spec.available), None)

    return ModelsOut(
        models=[
            ModelOut(
                id=spec.id,
                provider=spec.provider,
                label=spec.label,
                description=spec.description,
                available=spec.available,
                implemented=spec.implemented,
                note=spec.note,
                tags=spec.tags,
            )
            for spec in catalogue
        ],
        default=default,
    )


# --- Ask ----------------------------------------------------------------------


def _event(name: str, payload: dict) -> str:
    """One SSE frame. The blank line is what terminates it."""
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


async def _record_message(
    session: DbSession,
    *,
    message_id: uuid.UUID,
    chat: Chat,
    user_id: uuid.UUID,
    role: str,
    content: str,
    **extra: object,
) -> None:
    """
    Write a turn under an id the device chose, at most once.

    The id being client-minted is what stops the answer appearing twice — but
    it also means the row may already exist by the time this runs. Two ordinary
    ways: the app pushes the question through /sync while the answer is still
    streaming, and `askTutor` retries the whole request after a 401 with the
    same pair of ids. A blind INSERT then violates pk_messages and loses an
    answer the student has already read.

    So this is an upsert in spirit: look first, and insert inside a savepoint so
    a row that appears between the check and the write is a no-op rather than an
    aborted transaction. The existing row is left alone — it says the same
    thing, and the device's copy is the one the student is looking at.
    """
    existing = await session.scalar(
        select(MessageRow).where(
            MessageRow.id == message_id, MessageRow.user_id == user_id
        )
    )
    if existing is not None:
        return

    try:
        async with session.begin_nested():
            session.add(
                MessageRow(
                    id=message_id,
                    chat_id=chat.id,
                    user_id=user_id,
                    role=role,
                    content=content,
                    **extra,
                )
            )
            await session.flush()
    except IntegrityError:
        # Written between the check and the insert. Nothing to do.
        log.info("message_already_recorded", message_id=str(message_id), role=role)


async def _load_history(
    session: DbSession, chat_id: uuid.UUID | None, user_id: uuid.UUID
) -> list[Message]:
    """
    The last few turns of this conversation.

    Scoped to the caller. A chat id is client-minted, so without the user filter
    anyone could read anyone's conversation by guessing one.
    """
    if chat_id is None:
        return []

    rows = (
        await session.scalars(
            select(MessageRow)
            .where(MessageRow.chat_id == chat_id, MessageRow.user_id == user_id)
            .order_by(MessageRow.created_at.desc())
            .limit(_HISTORY_TURNS)
        )
    ).all()

    return [
        Message(role="assistant" if row.role == "tutor" else "user", content=row.content)
        for row in reversed(rows)
    ]


async def _ensure_chat(
    session: DbSession, chat_id: uuid.UUID | None, user_id: uuid.UUID, question: str
) -> Chat:
    """
    The conversation this turn belongs to, created if the client named a new one.

    Upsert on the client's id, like every other write in this API — so a retried
    request is one chat, not two.

    A id that exists but belongs to somebody else gets a fresh conversation
    instead. Two other readings were considered and are worse: filtering the
    lookup by owner and then inserting hits the primary key and fails the
    request with a 409, which both breaks a legitimate collision *and* confirms
    to a prober that the id is taken; returning 403 confirms it outright. This
    way nothing is revealed, nothing is read, and the student still gets an
    answer — the `meta` frame reports the id actually used, so a client with a
    genuine bug can see what happened.
    """
    if chat_id is not None:
        existing = await session.scalar(select(Chat).where(Chat.id == chat_id))

        if existing is not None:
            if existing.user_id == user_id:
                return existing

            log.warning("tutor_chat_id_taken", chat_id=str(chat_id), user_id=str(user_id))
            chat_id = None

    chat = Chat(
        id=chat_id or uuid.uuid4(),
        user_id=user_id,
        # The first question, trimmed. A student recognises their own words in
        # a list long before they recognise a date.
        title=question[:117] + "…" if len(question) > 118 else question,
    )
    session.add(chat)
    await session.flush()
    return chat


@router.post("/ask", summary="Ask the tutor")
async def ask(
    payload: AskRequest,
    user: CurrentUser,
    session: DbSession,
) -> StreamingResponse:
    """
    An answer, streamed as server-sent events.

    The frames, in order:

    * ``meta``   — the model that answered, the mode, and the sources. Sent
                   before the first token so the app can render the citation
                   header while the answer is still arriving.
    * ``token``  — a piece of the answer. Many of these.
    * ``done``   — the finished text, for a client that would rather keep the
                   whole thing than accumulate.
    * ``error``  — something went wrong mid-stream. See the note below.

    The quota is charged **before** generating, not after. Charging afterwards
    means a student who disconnects halfway has asked for free, and that is the
    cheapest possible way to bypass an allowance.
    """
    entitlement = await get_entitlement(session, user.id)
    await check_ai_query(session, user.id, entitlement)

    chat = await _ensure_chat(session, payload.chat_id, user.id, payload.question)
    history = await _load_history(session, chat.id, user.id)

    plan = await pipeline.plan(
        session,
        question=payload.question,
        user_id=user.id,
        unit_code=payload.unit_code,
        history=history,
        model_id=payload.model,
    )

    student_message_id = payload.student_message_id or uuid.uuid4()
    answer_message_id = payload.answer_message_id or uuid.uuid4()

    # Recorded before the answer exists. The student's turn is a fact whether or
    # not the tutor manages to reply, and a question with no row is a
    # conversation with a hole in it.
    await _record_message(
        session,
        message_id=student_message_id,
        chat=chat,
        user_id=user.id,
        role="student",
        content=payload.question,
    )
    await record_ai_query(session, user.id)
    await session.commit()

    sources = [
        SourceOut(
            material_id=passage.material_id,
            title=passage.title,
            unit_code=passage.unit_code,
            page_number=passage.page_number,
        )
        for passage in plan.passages
    ]

    async def frames() -> AsyncIterator[str]:
        collected: list[str] = []

        yield _event(
            "meta",
            {
                "chat_id": str(chat.id),
                "student_message_id": str(student_message_id),
                "answer_message_id": str(answer_message_id),
                "model": plan.spec.id,
                "model_label": plan.spec.label,
                "mode": plan.mode,
                "grounded": plan.mode == "grounded",
                "sources": [source.model_dump() for source in sources],
            },
        )

        try:
            async for piece in pipeline.generate(plan):
                collected.append(piece)
                yield _event("token", {"text": piece})

        except AppError as error:
            # The headers went out with the first frame, so there is no status
            # code left to change. The error has to travel *inside* the stream,
            # and the client shows it in place of the rest of the answer.
            log.warning("tutor_stream_error", error=error.message, chat_id=str(chat.id))
            yield _event("error", {"message": error.message})
            return

        except Exception:  # noqa: BLE001
            log.exception("tutor_stream_crashed", chat_id=str(chat.id))
            yield _event("error", {"message": "Something went wrong on our side."})
            return

        answer = "".join(collected).strip()
        if answer:
            await _save_answer(
                session, chat, user.id, plan, answer, message_id=answer_message_id
            )

        yield _event("done", {"text": answer})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers proxied responses by default, which holds the whole
            # answer back and delivers it in one lump — the exact thing
            # streaming exists to avoid.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _save_answer(
    session: DbSession,
    chat: Chat,
    user_id: uuid.UUID,
    plan: pipeline.Plan,
    answer: str,
    *,
    message_id: uuid.UUID,
) -> None:
    """
    Store the reply, with what it was built from.

    In its own try: the answer has already reached the student by this point, so
    a write failure here must not turn a delivered answer into an error frame.
    """
    prompt_text = "\n".join(message.content for message in plan.messages)
    usage = pipeline.estimate_usage(prompt_text, answer)

    try:
        await _record_message(
            session,
            message_id=message_id,
            chat=chat,
            user_id=user_id,
            role="tutor",
            content=answer,
            sources=[
                {
                    "material_id": str(passage.material_id),
                    "title": passage.title,
                    "page_number": passage.page_number,
                }
                for passage in plan.passages
            ]
            or None,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            model=plan.spec.id,
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("tutor_save_failed", chat_id=str(chat.id))
        await session.rollback()


# --- Quiz ---------------------------------------------------------------------


class QuizRequest(BaseModel):
    #: What to be quizzed on. Either this or `unit_code` is required — a quiz
    #: over nothing in particular is a quiz over nothing.
    topic: str | None = Field(default=None, max_length=200)
    unit_code: str | None = Field(default=None, max_length=16)
    #: Clamped to the plan's ceiling server-side. A client asking for fifty gets
    #: its plan's limit, not fifty.
    count: int | None = Field(default=None, ge=1, le=20)
    model: str | None = None


class QuestionOut(BaseModel):
    prompt: str
    options: list[str]
    #: Index into `options`. Sent with the question rather than held back: the
    #: app marks the answer on the device so a student can revise offline, and
    #: anyone determined enough to read it out of the response was going to look
    #: it up anyway.
    answer: int
    explanation: str
    source: str


class QuizOut(BaseModel):
    questions: list[QuestionOut]
    #: Whether these came from the student's own material.
    grounded: bool
    #: One line saying where the questions came from, shown above the quiz.
    note: str
    model: str


@router.post("/quiz", response_model=QuizOut, summary="Build a revision quiz")
async def build_quiz(
    payload: QuizRequest,
    user: CurrentUser,
    session: DbSession,
) -> QuizOut:
    """
    A multiple-choice quiz, from the student's material where it covers the
    topic and from general knowledge where it does not.

    Not streamed, unlike an answer. The app renders a quiz as cards and there is
    nothing to show until the last question is parsed, so streaming would buy a
    progress bar and cost the ability to validate before returning — and an
    unvalidated quiz can tell a student they are wrong when the question was
    broken.

    Metered against the plan's quiz allowance, which is monthly on Focus and
    lifetime on Free. Charged before generating, for the same reason `/ask`
    is: a client that disconnects halfway would otherwise quiz for free.
    """
    entitlement = await get_entitlement(session, user.id)
    await check_quiz(session, user.id, entitlement)

    ceiling = quiz_service.max_questions_for(entitlement)
    if ceiling == 0:
        raise AppError("Quizzes are not included in your plan.", status_code=402)

    count = quiz_service.clamp(payload.count, ceiling)

    built = await quiz_service.build(
        session,
        user_id=user.id,
        unit_code=payload.unit_code,
        topic=payload.topic,
        count=count,
        model_id=payload.model,
    )

    # After the build, so a provider failure does not spend an allowance the
    # student got nothing for. The window in which two concurrent requests
    # could both pass the check is one quiz wide, which is a cheaper problem
    # than charging for failures.
    await record_usage(session, user.id, quiz_metric(entitlement.limits))
    await session.commit()

    return QuizOut(
        questions=[
            QuestionOut(
                prompt=question.prompt,
                options=question.options,
                answer=question.answer,
                explanation=question.explanation,
                source=question.source,
            )
            for question in built.questions
        ],
        grounded=built.grounded,
        note=built.note,
        model=built.model,
    )
