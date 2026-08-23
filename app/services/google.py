from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.errors import AppError

#: Google's own verification endpoint. Using it rather than fetching JWKS and
#: validating locally: one HTTP call against an endpoint Google keeps correct
#: beats reimplementing signature checks, key rotation and clock skew here.
#: The tradeoff is a network round trip on a path that already has one.
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

#: Google will only ever issue tokens under one of these.
VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    name: str


async def verify_id_token(client: httpx.AsyncClient, id_token: str) -> GoogleIdentity:
    """
    Turns a Google ID token into an identity, or refuses.

    Three checks matter and all three are easy to skip:

    * **Audience.** Without it, an ID token minted for *any* Google app is
      accepted here — anyone with a Google login on any service could sign in
      as anyone. This is the check that makes the rest meaningful.
    * **Issuer.** Cheap, and closes off a forged token that got the audience
      right.
    * **Verified email.** An unverified Google address is a claim, not an
      identity, and accounts are keyed on it.
    """
    if not settings.google_client_ids:
        raise AppError("Google sign-in is not configured on this server.")

    response = await client.get(TOKENINFO_URL, params={"id_token": id_token})
    if response.status_code >= 400:
        raise AppError("That Google sign-in could not be verified.", status_code=401)

    claims = response.json()

    if claims.get("aud") not in settings.google_client_ids:
        raise AppError("That Google sign-in was issued for another app.", status_code=401)

    if claims.get("iss") not in VALID_ISSUERS:
        raise AppError("That Google sign-in could not be verified.", status_code=401)

    # tokeninfo returns strings, not booleans.
    verified = str(claims.get("email_verified", "false")).lower() == "true"
    email = (claims.get("email") or "").strip().lower()

    if not email or not verified:
        raise AppError(
            "That Google account has no verified email address.", status_code=401
        )

    return GoogleIdentity(
        subject=claims["sub"],
        email=email,
        email_verified=verified,
        name=(claims.get("name") or "").strip(),
    )
