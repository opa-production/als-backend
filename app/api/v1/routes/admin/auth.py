from fastapi import APIRouter, Request

from app.api.deps import ClientIp, CurrentAdmin, DbSession
from app.core.config import settings
from app.schemas.admin import (
    ActionResult,
    AdminLogin,
    AdminOut,
    AdminTokens,
    RefreshRequest,
)
from app.services import admin_auth
from app.services import audit as audit_service

router = APIRouter()


def _tokens(access: str, refresh: str, admin) -> AdminTokens:
    return AdminTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.admin_access_ttl_minutes * 60,
        admin=AdminOut.model_validate(admin),
    )


@router.post("/login", response_model=AdminTokens, summary="Sign in")
async def login(
    body: AdminLogin,
    session: DbSession,
    request: Request,
    ip: ClientIp,
) -> AdminTokens:
    """
    Email and password to a token pair.

    A wrong password and an unknown email give the same answer and take the
    same time — see ``app/services/admin_auth.py``. Anything else turns this
    form into a way to enumerate who has access.

    The successful login is written to the audit log. It is the one *read* this
    system records, because "who signed in, from where" is the first question
    anyone asks after something goes wrong.
    """
    admin = await admin_auth.authenticate(
        session, email=body.email, password=body.password
    )
    access, refresh = await admin_auth.issue_session(
        session,
        admin=admin,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )

    await audit_service.record(
        session,
        admin=admin,
        action="admin.signed_in",
        target_type="admin",
        target_id=admin.id,
        summary=f"{admin.email} signed in",
        ip=ip,
    )

    return _tokens(access, refresh, admin)


@router.post("/refresh", response_model=AdminTokens, summary="Rotate the session")
async def refresh(
    body: RefreshRequest, session: DbSession, ip: ClientIp
) -> AdminTokens:
    """
    A refresh token for a new pair. The old one is revoked in the same
    transaction, so a replay of it fails rather than opening a second session.
    """
    admin, access, new_refresh = await admin_auth.rotate_session(
        session, refresh_token=body.refresh_token, ip=ip
    )
    return _tokens(access, new_refresh, admin)


@router.post("/logout", response_model=ActionResult, summary="End this session")
async def logout(body: RefreshRequest, session: DbSession) -> ActionResult:
    """
    Revokes one refresh token.

    Deliberately not authenticated: a console that cannot sign out because its
    access token has already expired is a console people stop signing out of.
    Presenting the refresh token is the proof, and revoking an already-revoked
    or unknown token is a no-op rather than an error — there is nothing useful
    to learn from the difference and nothing to gain by refusing.
    """
    await admin_auth.revoke_session(session, refresh_token=body.refresh_token)
    return ActionResult(message="Signed out.")


@router.get("/me", response_model=AdminOut, summary="The signed-in admin")
async def me(admin: CurrentAdmin) -> AdminOut:
    return AdminOut.model_validate(admin)
