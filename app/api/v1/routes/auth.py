from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, HttpClient
from app.core.config import settings
from app.schemas.auth import (
    GoogleSignInRequest,
    LogoutRequest,
    OtpRequest,
    OtpRequestResponse,
    OtpVerifyRequest,
    RefreshRequest,
    TokenPair,
)
from app.services import auth as auth_service
from app.services.google import verify_id_token
from app.services.sms import get_sms_provider

router = APIRouter()


@router.post(
    "/otp",
    response_model=OtpRequestResponse,
    summary="Send a sign-in code",
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_otp(
    payload: OtpRequest, session: DbSession, http: HttpClient
) -> OtpRequestResponse:
    """
    Texts a six-digit code to the number.

    The response is the same whether or not the number has an account —
    anything else makes this a way to enumerate who is registered.

    With no SMS provider configured the code comes back in `debug_code` and is
    written to the server log, so the whole flow works in Swagger with no
    account and no credit. That field disappears the moment credentials exist.

    The store review number is the one exception: nothing is minted and nothing
    is sent, because its code is fixed and already in the review notes. The
    response is identical, so the endpoint still says nothing about which
    numbers are special.
    """
    code = await auth_service.request_otp(session, phone=payload.phone)

    if code is not None:
        provider = get_sms_provider(http)
        await provider.send(
            auth_service.normalise_phone(payload.phone),
            f"{code} is your ALS code. It expires in "
            f"{settings.otp_ttl_seconds // 60} minutes.",
        )

    return OtpRequestResponse(
        expires_in_seconds=settings.otp_ttl_seconds,
        debug_code=None if settings.sms_configured else code,
    )


@router.post("/otp/verify", response_model=TokenPair, summary="Sign in with a code")
async def verify_otp(payload: OtpVerifyRequest, session: DbSession) -> TokenPair:
    """
    Exchanges a code for tokens, creating the account on first use.

    There is no separate sign-up: a number that verifies and has no account
    gets one.

    The store review number signs in with its fixed code and is put back on a
    full, unexpired plan every time, so a reviewer never lands on the paywall.

    A **new** account gets the fourteen-day trial only if this number has never
    had one. Deleting an account and signing up again returns you to where you
    left off, not to a fresh fortnight.

    Signing in **takes over the account**: any other device is signed out
    immediately. One account, one phone.
    """
    existing = await auth_service.find_user_by_phone(session, payload.phone)

    user = await auth_service.verify_otp_code(
        session,
        phone=payload.phone,
        code=payload.code,
        device_id=payload.device_id,
    )

    device = await auth_service.register_device(
        session,
        user_id=user.id,
        device_id=payload.device_id,
        platform=payload.platform,
        app_version=payload.app_version,
    )

    access, refresh = await auth_service.issue_tokens(
        session, user=user, device_id=device.id
    )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user_id=user.id,
        is_new_user=existing is None,
    )


@router.post("/google", response_model=TokenPair, summary="Sign in with Google")
async def google_sign_in(
    payload: GoogleSignInRequest, session: DbSession, http: HttpClient
) -> TokenPair:
    """
    Verifies a Google ID token and signs the student in.

    The token is checked against Google — audience, issuer and a verified email
    — before anything is trusted. An account already created with a phone
    number is matched on that email rather than duplicated.
    """
    identity = await verify_id_token(http, payload.id_token)

    existing = await auth_service.find_user_by_email(session, identity.email)
    user = await auth_service.upsert_google_user(
        session,
        email=identity.email,
        full_name=identity.name,
        device_id=payload.device_id,
    )

    device = await auth_service.register_device(
        session,
        user_id=user.id,
        device_id=payload.device_id,
        platform=payload.platform,
        app_version=payload.app_version,
    )

    access, refresh = await auth_service.issue_tokens(
        session, user=user, device_id=device.id
    )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user_id=user.id,
        is_new_user=existing is None,
    )


@router.post("/refresh", response_model=TokenPair, summary="Get a new access token")
async def refresh(payload: RefreshRequest, session: DbSession) -> TokenPair:
    """
    Swaps a refresh token for a new pair.

    The old token is revoked in the same transaction. Rotation means a stolen
    token cannot be used alongside the real device indefinitely — one of the
    two starts failing, which is a signal rather than a silent shared session.
    """
    user, access, new_refresh = await auth_service.rotate_refresh_token(
        session, raw_token=payload.refresh_token
    )

    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.jwt_access_ttl_minutes * 60,
        user_id=user.id,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
)
async def logout(
    payload: LogoutRequest, user: CurrentUser, session: DbSession
) -> None:
    """
    Revokes refresh tokens for one device, or for all of them.

    Omitting `device_id` signs out everywhere, which is what someone reaches
    for when a phone is lost. The access token already held keeps working until
    it expires — it is short-lived by design, and tracking every one of them
    would mean a database read on every request for a rare case.
    """
    await auth_service.revoke_device_tokens(
        session, user_id=user.id, device_id=payload.device_id
    )
