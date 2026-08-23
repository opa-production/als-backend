import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from app.core.config import settings

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


def _now() -> datetime:
    return datetime.now(UTC)


# --- Tokens ------------------------------------------------------------------


def create_access_token(user_id: uuid.UUID, *, device_id: uuid.UUID | None = None) -> str:
    """
    Short-lived, and carries only an id.

    Nothing else goes in the payload. A token holding a plan tier or a name is
    a copy of state that starts going stale the moment it is signed, and a
    student who upgrades should not have to sign out to get what they paid for.
    """
    issued = _now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": ACCESS_TOKEN,
        "iat": int(issued.timestamp()),
        "exp": int(
            (issued + timedelta(minutes=settings.jwt_access_ttl_minutes)).timestamp()
        ),
    }
    if device_id:
        payload["did"] = str(device_id)

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Returns the claims, or None. Never raises — callers turn None into a 401."""
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None

    # A refresh token is also a valid signature. Without this check it would be
    # accepted as an access token and last sixty days instead of thirty minutes.
    if claims.get("typ") != ACCESS_TOKEN:
        return None

    return claims


def create_refresh_token() -> tuple[str, str]:
    """
    An opaque random string, and its hash.

    Not a JWT: a refresh token has to be revocable, and a self-contained token
    cannot be taken back before it expires. The plain value goes to the client
    once; only the hash is stored.
    """
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    """
    SHA-256, keyed with the app secret.

    Not bcrypt: these are 48 bytes of entropy, not a password, so there is
    nothing to brute-force and a slow hash on every refresh would be latency
    for nothing. Keyed so a leaked database alone is not enough to build a
    lookup table.
    """
    return hmac.new(
        settings.jwt_secret.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()


def refresh_expiry() -> datetime:
    return _now() + timedelta(days=settings.jwt_refresh_ttl_days)


# --- One-time codes ----------------------------------------------------------

OTP_LENGTH = 6


def create_otp() -> tuple[str, str]:
    """
    Six digits, from a cryptographic source, and its hash.

    ``secrets`` rather than ``random``: the latter's output is predictable from
    a few samples, which for a login code means predictable logins.
    """
    code = f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"
    return code, hash_token(code)


def verify_otp(code: str, code_hash: str) -> bool:
    """Constant-time, so timing cannot leak how much of a guess was right."""
    return hmac.compare_digest(hash_token(code), code_hash)
