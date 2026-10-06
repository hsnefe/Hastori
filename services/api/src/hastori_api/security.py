"""Access tokens (JWT, HS256) and password verification."""

import asyncio
import time
import uuid

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError

from hastori_api.errors import unauthorized
from hastori_common.settings import Settings

ALGORITHM = "HS256"
ISSUER = "hastori"
AUDIENCE = "hastori-api"

_hasher = PasswordHash.recommended()
_dummy_hash: str | None = None


def issue_access_token(settings: Settings, user_id: uuid.UUID, now: float | None = None) -> str:
    iat = int(now if now is not None else time.time())
    claims = {
        "sub": str(user_id),
        "iat": iat,
        "exp": iat + settings.access_token_ttl_s,
        "jti": uuid.uuid4().hex,
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> uuid.UUID:
    """The user the token was issued to. Signature, expiry, issuer, audience and the algorithm
    are all checked (the algorithm list is fixed: a token cannot choose `none`)."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        return uuid.UUID(claims["sub"])
    except jwt.ExpiredSignatureError:
        raise unauthorized("Access token expired") from None
    except (jwt.InvalidTokenError, ValueError):
        raise unauthorized("Invalid access token") from None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    """Always spends the time of one verification, also for a user that does not exist: the
    answer must not reveal which e-mail addresses have an account. Runs in a thread (argon2 is
    deliberately slow and would block the event loop)."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = await asyncio.to_thread(_hasher.hash, "hastori-no-such-user")
        await asyncio.to_thread(_verify, password, _dummy_hash)
        return False
    return await asyncio.to_thread(_verify, password, password_hash)


def _verify(password: str, password_hash: str) -> bool:
    try:
        return bool(_hasher.verify(password, password_hash))
    except PwdlibError:  # not a hash this library knows
        return False
