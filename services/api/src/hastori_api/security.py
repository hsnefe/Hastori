"""Access tokens (JWT, HS256) and password verification."""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError

from hastori_api.errors import HashQueueFull, unauthorized
from hastori_common.settings import Settings

ALGORITHM = "HS256"
ISSUER = "hastori"
AUDIENCE = "hastori-api"

_hasher = PasswordHash.recommended()
_dummy_hash: str | None = None

# One argon2 verification needs 64 MiB; sign-in is unauthenticated, so the number running at once
# is capped (the container has a 384 MiB limit). The rest wait, they are not refused.
MAX_CONCURRENT_HASHES = 2
_hash_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _slots() -> asyncio.Semaphore:
    global _hash_slots
    loop = asyncio.get_running_loop()
    if _hash_slots is None or _hash_slots[0] is not loop:  # a semaphore belongs to one loop
        _hash_slots = (loop, asyncio.Semaphore(MAX_CONCURRENT_HASHES))
    return _hash_slots[1]


# Sign-in is unauthenticated and each hash takes ~100 ms of a slot: an unbounded wait queue would
# let a flood of logins hold every request (and database connection) behind it. Beyond this many
# waiting hashes the answer is a fast 503.
MAX_PENDING_HASHES = 16
_pending = 0


@contextlib.asynccontextmanager
async def _hash_slot() -> AsyncIterator[None]:
    global _pending
    if _pending >= MAX_PENDING_HASHES:
        raise HashQueueFull
    _pending += 1
    try:
        async with _slots():
            yield
    finally:
        _pending -= 1


async def hash_password_async(password: str) -> str:
    async with _hash_slot():
        return await asyncio.to_thread(_hasher.hash, password)


def issue_access_token(
    settings: Settings, user_id: uuid.UUID, now: float | None = None, token_version: int = 0
) -> str:
    iat = int(now if now is not None else time.time())
    claims = {
        "tv": token_version,
        "sub": str(user_id),
        "iat": iat,
        "exp": iat + settings.access_token_ttl_s,
        "jti": uuid.uuid4().hex,
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> uuid.UUID:
    """The user the token was issued to."""
    return decode_access_claims(settings, token)[0]


def decode_access_claims(settings: Settings, token: str) -> tuple[uuid.UUID, int]:
    """The user and the token version the token was issued under. Signature, expiry, issuer,
    audience and the algorithm are all checked (the algorithm list is fixed: a token cannot
    choose `none`). A token from before versions existed counts as version 0."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        return uuid.UUID(claims["sub"]), int(claims.get("tv", 0))
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
            _dummy_hash = await hash_password_async("hastori-no-such-user")
        async with _hash_slot():
            await asyncio.to_thread(_verify, password, _dummy_hash)
        return False
    async with _hash_slot():
        return await asyncio.to_thread(_verify, password, password_hash)


def _verify(password: str, password_hash: str) -> bool:
    try:
        return bool(_hasher.verify(password, password_hash))
    except PwdlibError:  # not a hash this library knows
        return False
