"""
"Is my app out of date?" — the one endpoint the app asks on launch.

Deliberately unauthenticated. The build most likely to need forcing off the
network is one that is broken, and "broken" often means it cannot sign in. An
update check behind a token is an update check that does not reach the phones
that need it most.

Nothing here is user-specific, so there is nothing to leak: it returns the same
answer to everybody on a given platform and version — which is also why it is
safe to cache.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from app.api.deps import DbSession
from app.services import releases

router = APIRouter()


class ReleaseOut(BaseModel):
    #: The newest published build, or "" when nothing has been published.
    latest_version: str
    #: Something newer exists. The app shows a card the student can dismiss.
    update_available: bool
    #: This build must not carry on. The app shows a modal with no dismiss.
    #:
    #: Never inferred from being behind — see `app/services/releases.py`. It is
    #: True only where an administrator has raised the minimum past this build.
    update_required: bool
    #: Where Update goes.
    store_url: str
    #: What changed, written for a student.
    notes: str
    #: The floor, so the app can say "builds before 1.4.0 have stopped working"
    #: rather than only "update".
    minimum_version: str


@router.get("/release", response_model=ReleaseOut, summary="Is there an update")
async def read_release(
    session: DbSession,
    response: Response,
    platform: str = Query(description="ios | android"),
    version: str = Query(default="", description="The build asking, e.g. 1.3.2"),
) -> ReleaseOut:
    """
    Called on launch and on foreground.

    An unknown platform is answered rather than refused. A 4xx here would be a
    launch-path error on every future platform this app is built for, in
    exchange for nothing — the honest answer to "is there an update for a
    platform I have never published to" is no.
    """
    if platform not in releases.PLATFORMS:
        return ReleaseOut(
            latest_version="",
            update_available=False,
            update_required=False,
            store_url="",
            notes="",
            minimum_version="",
        )

    result = await releases.check(session, platform=platform, version=version)

    # Five minutes. Long enough that a launch spike does not become a query per
    # launch, short enough that forcing an update reaches everybody inside a
    # coffee break — which is the response time that decision actually needs.
    response.headers["Cache-Control"] = "public, max-age=300"

    return ReleaseOut(
        latest_version=result.latest_version,
        update_available=result.update_available,
        update_required=result.update_required,
        store_url=result.store_url,
        notes=result.notes,
        minimum_version=result.minimum_version,
    )
