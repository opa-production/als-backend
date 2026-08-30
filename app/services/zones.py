"""
Where a student is, for anything that has to happen at a wall-clock time.

Three parts of the service need this and none of them should own it. Quotas
turn over on the 1st *where the student is*. Reminders fire at eight in the
morning *where the student is*. The tutor is told what is on the timetable
today, and today ends at midnight *where the student is*. A copy of this rule
in each of them is three chances to read a preference differently, and the
symptom of a disagreement is a student being told they are out of questions on
a day their phone says has already started.

The preference itself lives on ``user_settings.timezone`` — see
``app/models/settings.py`` for why it is stored as an IANA name rather than an
offset.
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import UserSettings

log = structlog.get_logger()

UTC_ZONE = ZoneInfo("UTC")

#: Where a student who has never saved a timezone is assumed to be. Read off
#: the settings column rather than written out again, so the boundary someone
#: gets before they open Settings is the one they keep afterwards.
DEFAULT_ZONE_NAME: str = UserSettings.__table__.c.timezone.default.arg


def zone_for(name: str | None) -> ZoneInfo:
    """
    An IANA name as a zone, falling back rather than failing.

    A name this machine does not know is a bad preference, not an outage.
    Raising here would take out a quota check, and with it the whole tutor, on
    behalf of a typo the client let through.
    """
    try:
        return ZoneInfo(name or DEFAULT_ZONE_NAME)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown_timezone", timezone=name)
        return UTC_ZONE


async def user_zone(session: AsyncSession, user_id: uuid.UUID) -> ZoneInfo:
    """
    The timezone this student's days and months are cut on.

    One narrow read of an indexed column. Callers that need it more than once
    resolve it here and hand it down rather than asking again per meter.
    """
    name = await session.scalar(
        select(UserSettings.timezone).where(UserSettings.user_id == user_id)
    )
    return zone_for(name)
