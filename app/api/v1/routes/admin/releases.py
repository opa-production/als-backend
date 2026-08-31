"""
Publishing an app release, and deciding when an old one has to stop.

The adoption endpoint is the important half. Forcing an update is a decision
with a number behind it — how many people are still on the build you are about
to switch off — and without that number it gets made on a feeling. Devices
already report their version on ``PUT /me/devices``, so the count is free.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminRole, ClientIp, CurrentAdmin, DbSession
from app.core.clock import now as utc_now
from app.core.errors import AppError, NotFound
from app.models.account import Device
from app.models.release import AppRelease
from app.services import audit as audit_service
from app.services import releases

router = APIRouter()


class ReleaseIn(BaseModel):
    platform: str = Field(description="ios | android")
    version: str = Field(max_length=32, description="As the store shows it, e.g. 1.4.0")
    #: The oldest build still allowed to run once this release is published.
    #:
    #: Blank is the normal case and means nobody is forced: the app offers a
    #: dismissible card and nothing else. Setting it is the deliberate act of
    #: locking people out of a build, and it is the only thing that does.
    minimum_version: str = Field(default="", max_length=32)
    store_url: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=1000)
    published: bool = False

    @field_validator("platform")
    @classmethod
    def _known_platform(cls, value: str) -> str:
        if value not in releases.PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(releases.PLATFORMS)}")
        return value


class ReleaseUpdate(BaseModel):
    minimum_version: str | None = Field(default=None, max_length=32)
    store_url: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=1000)
    published: bool | None = None


class ReleaseOut(BaseModel):
    id: uuid.UUID
    platform: str
    version: str
    minimum_version: str
    store_url: str
    notes: str
    published: bool
    #: True for the one row per platform that clients are currently being told
    #: about. Derived, never stored — two rows that both believe they are
    #: current is a state worth making unrepresentable.
    is_current: bool


class AdoptionRow(BaseModel):
    platform: str
    version: str
    devices: int


def _out(row: AppRelease, current_id: uuid.UUID | None) -> ReleaseOut:
    return ReleaseOut(
        id=row.id,
        platform=row.platform,
        version=row.version,
        minimum_version=row.minimum_version,
        store_url=row.store_url,
        notes=row.notes,
        published=row.published,
        is_current=row.id == current_id,
    )


async def _current_ids(session: AsyncSession) -> set[uuid.UUID]:
    """The id of the release each platform is currently offering."""
    ids = set()
    for platform in releases.PLATFORMS:
        row = await releases.latest(session, platform)
        if row is not None:
            ids.add(row.id)
    return ids


@router.get("", response_model=list[ReleaseOut], summary="Every release")
async def list_releases(session: DbSession, admin: CurrentAdmin) -> list[ReleaseOut]:
    """
    Newest first, per platform.

    Sorted here rather than in SQL for the reason in
    ``app/services/releases.py``: the database compares versions as text, and
    text says 1.10.0 comes before 1.9.0.
    """
    rows = (await session.scalars(select(AppRelease))).all()
    current = await _current_ids(session)

    ordered = sorted(
        rows,
        key=lambda row: (row.platform, releases.parse(row.version)),
        reverse=True,
    )
    return [_out(row, row.id if row.id in current else None) for row in ordered]


@router.get(
    "/adoption",
    response_model=list[AdoptionRow],
    summary="Which builds are actually out there",
)
async def adoption(session: DbSession, admin: CurrentAdmin) -> list[AdoptionRow]:
    """
    Registered devices per version.

    Read before raising ``minimum_version``: this is the count of people the
    change locks out until they update, and it is the difference between a
    forced update and an outage.

    Counts devices, not accounts. One student with a phone and a tablet is two
    rows here, which is the right unit — each one has to be updated separately.
    """
    rows = (
        await session.execute(
            select(Device.platform, Device.app_version, func.count()).group_by(
                Device.platform, Device.app_version
            )
        )
    ).all()

    return sorted(
        (
            AdoptionRow(
                platform=platform or "unknown",
                # An empty string is a real answer, not missing data: it is a
                # build old enough that it predates reporting the field.
                version=version or "unreported",
                devices=int(count),
            )
            for platform, version, count in rows
        ),
        key=lambda row: (row.platform, releases.parse(row.version)),
        reverse=True,
    )


@router.post("", response_model=ReleaseOut, status_code=201, summary="Add a release")
async def create_release(
    body: ReleaseIn,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
) -> ReleaseOut:
    """
    Records a build. Publishing it is a separate decision, and defaults to no.

    A release usually exists before it is downloadable — the store is still
    reviewing, or rolling out to 10% — and offering an update that cannot yet
    be installed sends people to a page that does not have it.
    """
    existing = await session.scalar(
        select(AppRelease).where(
            AppRelease.platform == body.platform,
            AppRelease.version == body.version,
        )
    )
    if existing is not None:
        raise AppError(f"{body.platform} {body.version} is already recorded.")

    if body.minimum_version and releases.is_older(body.version, body.minimum_version):
        # Otherwise every client is told to update to a build that is itself
        # below the floor, including the one they would be updating to.
        raise AppError("The minimum version cannot be newer than the release itself.")

    release = AppRelease(
        platform=body.platform,
        version=body.version,
        minimum_version=body.minimum_version,
        store_url=body.store_url,
        notes=body.notes,
        published=body.published,
        published_at=utc_now() if body.published else None,
    )
    session.add(release)
    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="release.created",
        target_type="release",
        target_id=release.id,
        summary=f"Recorded {release.platform} {release.version}"
        + (" and published it" if release.published else ""),
        meta={
            "platform": release.platform,
            "version": release.version,
            "minimum_version": release.minimum_version,
            "published": release.published,
        },
        ip=ip,
    )

    return _out(release, release.id if release.published else None)


@router.patch("/{release_id}", response_model=ReleaseOut, summary="Change a release")
async def update_release(
    release_id: uuid.UUID,
    body: ReleaseUpdate,
    session: DbSession,
    admin: AdminRole,
    ip: ClientIp,
) -> ReleaseOut:
    """
    Publishing, un-publishing, and raising the floor.

    Raising ``minimum_version`` is the one action here that reaches into a
    student's hands and stops them working, so it is audited with the old value
    beside the new one. A log entry saying "changed the minimum" answers
    nothing at 2am; one saying "1.2.0 became 1.4.0" answers everything.
    """
    release = await session.get(AppRelease, release_id)
    if release is None:
        raise NotFound("No such release.")

    changes = body.model_dump(exclude_unset=True)

    if changes.get("minimum_version") and releases.is_older(
        release.version, changes["minimum_version"]
    ):
        raise AppError("The minimum version cannot be newer than the release itself.")

    before = {key: getattr(release, key) for key in changes}

    for field_name, value in changes.items():
        setattr(release, field_name, value)

    if changes.get("published") and release.published_at is None:
        release.published_at = utc_now()

    await session.flush()

    await audit_service.record(
        session,
        admin=admin,
        action="release.updated",
        target_type="release",
        target_id=release.id,
        summary=f"Updated {release.platform} {release.version}: "
        + ", ".join(
            f"{key} was {before[key]!r}, now {value!r}"
            for key, value in changes.items()
        ),
        meta={
            "before": {key: str(value) for key, value in before.items()},
            "after": {key: str(value) for key, value in changes.items()},
        },
        ip=ip,
    )

    current = await _current_ids(session)
    return _out(release, release.id if release.id in current else None)
