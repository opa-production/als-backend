"""
Deciding whether the app on this phone is out of date.

Two questions, and they are not the same one:

* **Is there something newer?** — the app shows a dismissible "what's new"
  card, and the student carries on.
* **Is this build too old to keep running?** — the app shows a modal with no
  way past it.

The second is a serious thing to do to somebody, usually mid-revision, so it is
never inferred. It happens only when an administrator has raised
``minimum_version`` past the build in their hand, or marked a release mandatory.
Being merely behind is not a reason to lock anyone out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.release import AppRelease

#: The platforms a build can be published for.
PLATFORMS = ("ios", "android")

_NUMBER = re.compile(r"\d+")


def parse(version: str | None) -> tuple[int, ...]:
    """
    A version as something that can be compared.

    Every run of digits, in order: ``"1.10.2"`` becomes ``(1, 10, 2)``. That is
    what makes 1.10 newer than 1.9, which string comparison gets backwards and
    which is the single most common way a version gate ships broken.

    Anything non-numeric is skipped rather than rejected, so ``"1.4.0-beta.2"``
    reads as ``(1, 4, 0, 2)``. This deliberately does not implement semver
    pre-release ordering — a beta sorting *after* its release is wrong by the
    spec and right here, because the beta is the later build and the person
    holding it should not be told to go back.

    An unreadable or missing version is ``()``, which compares below every real
    version. A client that will not say what it is is treated as ancient: it is
    almost always a very old build whose reporting predates the field.
    """
    if not version:
        return ()
    return tuple(int(part) for part in _NUMBER.findall(version)[:6])


def is_older(version: str | None, than: str | None) -> bool:
    """
    Whether ``version`` is behind ``than``.

    Padding matters and Python's tuple comparison already does it correctly for
    the case that bites: ``(1, 4) < (1, 4, 1)`` is True, so "1.4" is older than
    "1.4.1" rather than equal to it.
    """
    if not than:
        return False
    return parse(version) < parse(than)


@dataclass(frozen=True)
class ReleaseCheck:
    """What one client should be told, in one answer."""

    #: The newest published build for this platform, or "" if none is.
    latest_version: str
    #: True when there is something newer to offer. Dismissible.
    update_available: bool
    #: True when this build must not carry on. Not dismissible.
    update_required: bool
    store_url: str
    notes: str
    minimum_version: str


def store_url_for(platform: str) -> str:
    """The fallback store link, so the usual release needs no URL typed in."""
    return {
        "ios": settings.ios_store_url,
        "android": settings.android_store_url,
    }.get(platform, "")


async def latest(session: AsyncSession, platform: str) -> AppRelease | None:
    """
    The newest *published* release for a platform.

    Ordered in Python, not in SQL. The database would sort "1.10.0" before
    "1.9.0" — it is comparing text — and this table is a handful of rows per
    platform, so reading them all and sorting them properly costs nothing and
    removes the one place this could silently be wrong.
    """
    rows = (
        await session.scalars(
            select(AppRelease).where(
                AppRelease.platform == platform,
                AppRelease.published.is_(True),
            )
        )
    ).all()

    if not rows:
        return None

    return max(rows, key=lambda row: parse(row.version))


async def check(
    session: AsyncSession, *, platform: str, version: str | None
) -> ReleaseCheck:
    """
    What to tell one client on one platform running one build.

    Nothing published means nothing to say — every flag is False and the app
    shows no card and no modal. That is the state this ships in, and it is the
    right default: an update prompt that appears because a table is empty is
    the failure mode worth designing out.
    """
    release = await latest(session, platform)

    if release is None:
        return ReleaseCheck(
            latest_version="",
            update_available=False,
            update_required=False,
            store_url=store_url_for(platform),
            notes="",
            minimum_version="",
        )

    behind = is_older(version, release.version)

    return ReleaseCheck(
        latest_version=release.version,
        update_available=behind,
        # Only ever True when someone decided it should be. `minimum_version`
        # is the normal lever; `mandatory` on the release itself is not one,
        # because a release that must be taken is exactly a release whose
        # minimum is itself — and expressing it once means there is no way for
        # the two to disagree.
        update_required=behind and is_older(version, release.minimum_version),
        store_url=release.store_url or store_url_for(platform),
        notes=release.notes,
        minimum_version=release.minimum_version,
    )
