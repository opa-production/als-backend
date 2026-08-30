"""
What students ask us to build.

The whole feature is one text box. That is deliberate: every field added to
this form is a reason not to fill it in, and the thing being collected is not a
structured record — it is the sentence somebody types the moment the app cannot
do what they opened it for. Categories, titles and votes can all be added later
from the paragraphs; a paragraph cannot be recovered from a dropdown nobody
used.

Nothing here is shown back to anybody — not to other students, and not to the
student who wrote it. Submitting is the whole interaction: the box closes, a
modal says thank you, and the paragraph is read in the console. A list of "your
requests" would be a screen whose only honest state is a row with no answer
beside it, which reads as being ignored rather than as being heard.
"""

import uuid
from datetime import timedelta

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.core.clock import now as utc_now
from app.core.errors import AppError
from app.models.feedback import FeatureRequest

log = structlog.get_logger()

router = APIRouter()

#: Long enough for a real thought, short enough that the console stays
#: readable. Anyone with more to say than this has more to say than a form.
MAX_BODY_CHARS = 2000

#: Below this it is not a request, it is a stray tap — "pls", "hi", an empty
#: box submitted twice. Rejected with a message that says what to write.
MIN_BODY_CHARS = 10

#: Per student, per day. High enough that nobody with genuine feedback ever
#: meets it, low enough that a retry loop in the app cannot fill the table.
MAX_PER_DAY = 5


class FeatureRequestIn(BaseModel):
    body: str = Field(max_length=MAX_BODY_CHARS)
    #: Optional context from the client. Neither is trusted for anything; they
    #: are here so a report can be read against the build it came from.
    app_version: str = Field(default="", max_length=32)
    platform: str = Field(default="", max_length=16)


class FeatureRequestOut(BaseModel):
    #: What the modal says. Sent by the server rather than held in the app so
    #: the wording can change without a release -- and so it can differ from
    #: the refusals above, which the same modal has to be able to show.
    message: str
    #: Not shown to anyone. It is here so a student who follows something up on
    #: WhatsApp can be matched to their row without searching the text.
    id: uuid.UUID


@router.post(
    "/feature-requests",
    response_model=FeatureRequestOut,
    status_code=201,
    summary="Ask for a feature",
)
async def create_feature_request(
    payload: FeatureRequestIn, user: CurrentUser, session: DbSession
) -> FeatureRequestOut:
    """
    Files one request, in the student's own words.

    Fire and forget: there is no companion ``GET``. The response exists to let
    the app close the sheet and show a modal, and carries an id for support
    rather than for the client to store.
    """
    body = payload.body.strip()

    if len(body) < MIN_BODY_CHARS:
        raise AppError(
            "Tell us a little more about what you would like the app to do."
        )

    since = utc_now() - timedelta(days=1)
    recent = (
        await session.scalar(
            select(func.count())
            .select_from(FeatureRequest)
            .where(
                FeatureRequest.user_id == user.id,
                FeatureRequest.created_at > since,
            )
        )
    ) or 0

    if recent >= MAX_PER_DAY:
        raise AppError(
            "Thanks — that is a few ideas already today. Send more tomorrow.",
            status_code=429,
        )

    row = FeatureRequest(
        user_id=user.id,
        body=body,
        app_version=payload.app_version.strip(),
        platform=payload.platform.strip(),
    )
    session.add(row)
    await session.flush()

    # Logged as an event as well as a row: the console is where these are read,
    # but a spike in requests after a release is something worth seeing in the
    # log stream without anyone opening a table.
    log.info(
        "feature_request_filed",
        user_id=str(user.id),
        chars=len(body),
        platform=row.platform,
        app_version=row.app_version,
    )

    return FeatureRequestOut(
        message="Thanks — your idea is with the team.", id=row.id
    )
