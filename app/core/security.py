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


# --- Admin credentials -------------------------------------------------------
#
# Admins have a password; students never do. Everything below is only reachable
# from ``/api/v1/admin``.

ADMIN_TOKEN = "admin"

#: scrypt from the standard library, rather than bcrypt or argon2 from a
#: dependency. It is a memory-hard KDF in the same family, it is what `hashlib`
#: already ships, and the alternative is adding passlib plus a C extension to
#: this service for a table that will hold single digits of rows.
#:
#: n=2**15, r=8, p=1 costs roughly 100ms and 32MB per verification — deliberate,
#: since the whole point of a KDF is to be slow, and nobody logs into an admin
#: console in a loop.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DK_LEN = 32

#: OpenSSL refuses a scrypt call that would allocate more than its own
#: ``maxmem``, which defaults to exactly 32MB — and n=2**15, r=8 needs
#: 128 * n * r = 32MB plus working space. Without this the KDF raises
#: "memory limit exceeded" rather than being slow, which is a very confusing
#: way for a login to fail.
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """
    ``scrypt$n$r$p$salt$hash``, salt and parameters carried with the digest.

    Storing the parameters means they can be raised later without invalidating
    every existing password — an old hash still says how to verify itself.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DK_LEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            salt.hex(),
            digest.hex(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time, and False rather than an exception on a malformed hash."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False

    return hmac.compare_digest(candidate.hex(), digest_hex)


def create_admin_token(admin_id: uuid.UUID) -> str:
    """
    Short-lived, and carries no role.

    The role is read from the row on every request. A role baked into a token
    means a demotion does not take effect until the token expires, which is the
    wrong direction for the one credential that can grant paid plans.
    """
    issued = _now()
    payload: dict[str, Any] = {
        "sub": str(admin_id),
        "typ": ADMIN_TOKEN,
        "iat": int(issued.timestamp()),
        "exp": int(
            (issued + timedelta(minutes=settings.admin_access_ttl_minutes)).timestamp()
        ),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_admin_token(token: str) -> dict[str, Any] | None:
    """
    Returns the claims, or None.

    The ``typ`` check is the whole point of this being a separate function: a
    student's access token is signed with the same secret and would otherwise
    verify perfectly here.
    """
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        return None

    if claims.get("typ") != ADMIN_TOKEN:
        return None

    return claims


def admin_refresh_expiry() -> datetime:
    return _now() + timedelta(days=settings.admin_refresh_ttl_days)
