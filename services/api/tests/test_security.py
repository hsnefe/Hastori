"""Access tokens, password checks, the login limiter and refresh token rotation (Lua on Redis)."""

import asyncio
import time
import uuid
from typing import Any

import jwt
import pytest
from redis.asyncio import Redis

from hastori_api.errors import ApiError
from hastori_api.ratelimit import LoginLimiter
from hastori_api.security import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    decode_access_token,
    hash_password,
    issue_access_token,
    verify_password,
)
from hastori_api.tokens import RefreshStore
from hastori_common.settings import Settings

USER = uuid.UUID(int=42)


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"jwt_secret": "s" * 40, "access_token_ttl_s": 900}
    return Settings.model_validate(base | over)


def forged(claims: dict[str, Any], key: str = "s" * 40, algorithm: str = ALGORITHM) -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


def good_claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "sub": str(USER),
        "iat": now,
        "exp": now + 900,
        "jti": "x",
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    return base | over


# -- access tokens ----------------------------------------------------------------------------


def test_a_fresh_token_names_its_user() -> None:
    s = settings()
    assert decode_access_token(s, issue_access_token(s, USER)) == USER


def test_each_token_has_its_own_id_and_the_expected_lifetime() -> None:
    s = settings()
    a, b = issue_access_token(s, USER), issue_access_token(s, USER)
    ca = jwt.decode(a, options={"verify_signature": False})
    cb = jwt.decode(b, options={"verify_signature": False})
    assert ca["jti"] != cb["jti"]
    assert ca["exp"] - ca["iat"] == 900 and ca["iss"] == ISSUER and ca["aud"] == AUDIENCE


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(forged(good_claims(exp=int(time.time()) - 5)), id="expired"),
        pytest.param(forged(good_claims(), key="other-key-" * 5), id="wrong signature"),
        pytest.param(forged(good_claims(aud="somebody-else")), id="wrong audience"),
        pytest.param(forged(good_claims(iss="not-hastori")), id="wrong issuer"),
        pytest.param(forged({k: v for k, v in good_claims().items() if k != "exp"}), id="no exp"),
        pytest.param(forged({k: v for k, v in good_claims().items() if k != "sub"}), id="no sub"),
        pytest.param(forged(good_claims(sub="not-a-uuid")), id="bad subject"),
        pytest.param("not.a.jwt", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(ApiError) as exc:
        decode_access_token(settings(), token)
    assert exc.value.status == 401


def test_a_token_cannot_choose_the_none_algorithm() -> None:
    unsigned = jwt.encode(good_claims(), key="", algorithm="none")
    with pytest.raises(ApiError):
        decode_access_token(settings(), unsigned)


def test_a_token_signed_with_another_hmac_algorithm_is_refused() -> None:
    with pytest.raises(ApiError):
        decode_access_token(settings(), forged(good_claims(), algorithm="HS512"))


def test_an_expired_token_says_so() -> None:
    with pytest.raises(ApiError) as exc:
        decode_access_token(settings(), forged(good_claims(exp=int(time.time()) - 5)))
    assert "expired" in exc.value.message


# -- passwords --------------------------------------------------------------------------------


async def test_password_verification() -> None:
    h = hash_password("correct horse")
    assert await verify_password("correct horse", h)
    assert not await verify_password("wrong", h)
    assert not await verify_password("anything", None)  # no such user
    assert not await verify_password("anything", "not-a-hash")  # a value the seed never makes


async def test_only_a_few_hashes_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each argon2 run needs 64 MiB and sign-in has no credentials: the rest must queue."""
    import threading
    import time

    from hastori_api import security

    running = peak = 0
    lock = threading.Lock()

    def slow_verify(password: str, password_hash: str) -> bool:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return False

    monkeypatch.setattr(security, "_verify", slow_verify)
    h = hash_password("x")
    results = await asyncio.gather(*(verify_password("y", h) for _ in range(12)))
    assert results == [False] * 12  # all of them are answered, none refused
    assert 1 <= peak <= security.MAX_CONCURRENT_HASHES


# -- login limiter ----------------------------------------------------------------------------


async def test_the_limiter_blocks_after_five_failures_and_only_that_address(
    fake_redis: Redis,
) -> None:
    limiter = LoginLimiter(fake_redis, max_failures=5, window_s=300)
    for _ in range(4):
        await limiter.failed("a@x.io", "1.1.1.1")
    assert await limiter.blocked_for("a@x.io", "1.1.1.1") == 0
    await limiter.failed("a@x.io", "1.1.1.1")
    assert 0 < await limiter.blocked_for("a@x.io", "1.1.1.1") <= 300
    assert await limiter.blocked_for("a@x.io", "2.2.2.2") == 0  # another address
    assert await limiter.blocked_for("b@x.io", "1.1.1.1") == 0  # another account


async def test_a_successful_login_resets_the_counter(fake_redis: Redis) -> None:
    limiter = LoginLimiter(fake_redis, max_failures=3, window_s=300)
    for _ in range(2):
        await limiter.failed("a@x.io", "1.1.1.1")
    await limiter.succeeded("a@x.io", "1.1.1.1")
    for _ in range(2):
        await limiter.failed("a@x.io", "1.1.1.1")
    assert await limiter.blocked_for("a@x.io", "1.1.1.1") == 0


async def test_the_failure_window_starts_at_the_first_failure_and_expires(
    fake_redis: Redis,
) -> None:
    limiter = LoginLimiter(fake_redis, max_failures=2, window_s=300)
    await limiter.failed("a@x.io", "1.1.1.1")
    await limiter.failed("a@x.io", "1.1.1.1")
    key = limiter._key("a@x.io", "1.1.1.1")
    assert 290 < await fake_redis.ttl(key) <= 300  # not extended by the second failure
    await fake_redis.delete(key)  # the window has passed
    assert await limiter.blocked_for("a@x.io", "1.1.1.1") == 0


async def test_the_limiter_never_stores_the_address_of_an_account(fake_redis: Redis) -> None:
    limiter = LoginLimiter(fake_redis)
    await limiter.failed("someone@secret.example", "1.1.1.1")
    keys = await fake_redis.keys("*")
    assert keys and all("someone" not in k and "secret" not in k for k in keys)


# -- refresh tokens ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def store_for(redis: Redis, clock: Clock, grace_s: int = 20) -> RefreshStore:
    return RefreshStore(redis, ttl_s=7 * 86400, grace_s=grace_s, clock=clock)


async def test_a_login_stores_only_the_hash_of_the_token(fake_redis: Redis) -> None:
    store = store_for(fake_redis, Clock())
    token = await store.issue(USER)
    keys = await fake_redis.keys("*")
    assert all(token not in k for k in keys)
    assert await fake_redis.hget(f"rt:{store.digest(token)}", "uid") == str(USER)
    assert 0 < await fake_redis.ttl(f"rt:{store.digest(token)}") <= 7 * 86400


async def test_rotation_issues_a_new_token_and_marks_the_old_one_used(fake_redis: Redis) -> None:
    clock = Clock()
    store = store_for(fake_redis, clock)
    first = await store.issue(USER)
    result = await store.rotate(first)
    assert result.status == "ok" and result.user_id == USER
    assert result.token and result.token != first
    assert await fake_redis.hget(f"rt:{store.digest(first)}", "used_at") == repr(clock.now)
    # the new token works, and keeps the family
    again = await store.rotate(result.token)
    assert again.status == "ok"
    fam = str(await fake_redis.hget(f"rt:{store.digest(first)}", "fid"))
    assert await fake_redis.scard(f"fam:{fam}") == 3


async def test_an_unknown_token_is_not_accepted(fake_redis: Redis) -> None:
    result = await store_for(fake_redis, Clock()).rotate("never-issued")
    assert result.status == "missing" and result.token is None


async def test_two_tabs_refreshing_at_once_both_succeed(fake_redis: Redis) -> None:
    clock = Clock()
    store = store_for(fake_redis, clock)
    token = await store.issue(USER)
    a = await store.rotate(token)
    clock.now += 3  # the second tab sends the same old token a moment later
    b = await store.rotate(token)
    assert a.status == "ok" and b.status == "ok" and a.token != b.token
    # both new tokens belong to the same family and both work
    assert (await store.rotate(a.token or "")).status == "ok"
    assert (await store.rotate(b.token or "")).status == "ok"


async def test_a_used_token_after_the_grace_window_revokes_the_whole_family(
    fake_redis: Redis,
) -> None:
    clock = Clock()
    store = store_for(fake_redis, clock, grace_s=20)
    stolen = await store.issue(USER)
    legit = await store.rotate(stolen)  # the real owner refreshed
    assert legit.status == "ok"
    clock.now += 21  # the thief tries the old token later
    theft = await store.rotate(stolen)
    assert theft.status == "reused" and theft.user_id == USER
    # the owner's newest token is gone too: everybody signs in again
    assert (await store.rotate(legit.token or "")).status == "missing"
    assert await fake_redis.keys("fam:*") == []


async def test_the_grace_window_is_exactly_the_configured_length(fake_redis: Redis) -> None:
    clock = Clock()
    store = store_for(fake_redis, clock, grace_s=20)
    token = await store.issue(USER)
    await store.rotate(token)
    clock.now += 20
    assert (await store.rotate(token)).status == "ok"
    clock.now += 0.001
    assert (await store.rotate(token)).status == "reused"


async def test_parallel_rotations_of_one_token_are_all_consistent(fake_redis: Redis) -> None:
    """Five requests with the same token at the same instant: none is lost, none is 'reused'."""
    import asyncio

    clock = Clock()
    store = store_for(fake_redis, clock)
    token = await store.issue(USER)
    results = await asyncio.gather(*[store.rotate(token) for _ in range(5)])
    assert [r.status for r in results] == ["ok"] * 5
    assert len({r.token for r in results}) == 5


async def test_logout_ends_every_token_of_the_session(fake_redis: Redis) -> None:
    clock = Clock()
    store = store_for(fake_redis, clock)
    first = await store.issue(USER)
    newest = (await store.rotate(first)).token or ""
    await store.revoke(newest)
    assert (await store.rotate(newest)).status == "missing"
    assert (await store.rotate(first)).status == "missing"
    assert await fake_redis.keys("*") == []


async def test_logout_with_an_unknown_token_changes_nothing(fake_redis: Redis) -> None:
    store = store_for(fake_redis, Clock())
    kept = await store.issue(USER)
    await store.revoke("never-issued")
    assert (await store.rotate(kept)).status == "ok"


async def test_another_users_session_is_not_touched_by_a_revocation(fake_redis: Redis) -> None:
    store = store_for(fake_redis, Clock())
    mine, theirs = await store.issue(USER), await store.issue(uuid.UUID(int=43))
    await store.revoke(mine)
    assert (await store.rotate(theirs)).status == "ok"
