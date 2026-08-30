"""
What students ask us to build.

The whole feature is one text box. That is deliberate: every field added to
this form is a reason not to fill it in, and the thing being collected is not a
structured record — it is the sentence somebody types the moment the app cannot
do what they opened it for. Categories, titles and votes can all be added later
from the paragraphs; a paragraph cannot be recovered from a dropdown nobody
used.

Nothing here is shown to other students, so there is no moderation surface, no
ranking and no reply. It is read in the console.
"""

import uuid
from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
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
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    body: str
    created_at: datetime


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

    The row is returned rather than a bare acknowledgement so the profile
    screen can show what was sent without a second call — and so a student can
    see their own words came through, which is most of what makes anyone send
    a second one.
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

    return FeatureRequestOut.model_validate(row)


@router.get(
    "/feature-requests",
    response_model=list[FeatureRequestOut],
    summary="What you have asked for",
)
async def read_feature_requests(
    user: CurrentUser, session: DbSession
) -> list[FeatureRequestOut]:
    """
    A student's own requests, newest first.

    Only theirs. This is not a public board — showing everyone's ideas would
    turn a feedback box into a forum, with everything that has to be moderated
    in one.
    """
    rows = await session.scalars(
        select(FeatureRequest)
        .where(FeatureRequest.user_id == user.id)
        .order_by(FeatureRequest.created_at.desc())
        .limit(MAX_PER_DAY * 10)
    )
    return [FeatureRequestOut.model_validate(row) for row in rows]
