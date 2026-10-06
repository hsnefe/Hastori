"""Refresh tokens: opaque, kept in Redis as a hash, rotated on every use.

Each login starts a *family*. Using a refresh token marks it used and issues a new member of the
family. Presenting a token that is already used is normal for a short grace window (two browser
tabs refreshing at once) and gets a new member too; after the window it can only mean a stolen
copy, so the whole family is revoked and everyone holding it signs in again.

The rotation is one Lua script: checking, marking and issuing happen atomically, so two parallel
requests cannot both be "the first". (The script touches keys it builds itself, which a Redis
Cluster would not allow; this runs on a single Redis.)
"""

import hashlib
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

RT = "rt:"
FAM = "fam:"

ROTATE_LUA = """
local rec = redis.call('HGETALL', KEYS[1])
if #rec == 0 then return {'missing'} end
local h = {}
for i = 1, #rec, 2 do h[rec[i]] = rec[i + 1] end
local now, grace = tonumber(ARGV[1]), tonumber(ARGV[2])
local rt, fam = ARGV[5], ARGV[6]
local fam_key = fam .. h['fid']
if h['used_at'] ~= '' then
  if now - tonumber(h['used_at']) > grace then
    for _, m in ipairs(redis.call('SMEMBERS', fam_key)) do redis.call('DEL', rt .. m) end
    redis.call('DEL', fam_key)
    return {'reused', h['uid']}
  end
else
  redis.call('HSET', KEYS[1], 'used_at', ARGV[1])
end
local new_key = rt .. ARGV[3]
redis.call('HSET', new_key, 'uid', h['uid'], 'fid', h['fid'], 'used_at', '')
redis.call('EXPIRE', new_key, ARGV[4])
redis.call('SADD', fam_key, ARGV[3])
redis.call('EXPIRE', fam_key, ARGV[4])
return {'ok', h['uid'], h['fid']}
"""


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class Rotation:
    status: Literal["ok", "missing", "reused"]
    user_id: uuid.UUID | None = None
    token: str | None = None  # the new refresh token, when status is "ok"


class RefreshStore:
    def __init__(
        self,
        redis: Redis,
        ttl_s: int,
        grace_s: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        self.grace_s = grace_s
        self.clock = clock
        self._rotate = redis.register_script(ROTATE_LUA)

    @staticmethod
    def digest(token: str) -> str:
        """Only this hash is stored: a leaked Redis dump holds no usable token."""
        return hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, user_id: uuid.UUID) -> str:
        """A new family for a fresh login."""
        token, family = secrets.token_urlsafe(32), uuid.uuid4().hex
        digest = self.digest(token)
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(RT + digest, mapping={"uid": str(user_id), "fid": family, "used_at": ""})
        pipe.expire(RT + digest, self.ttl_s)
        pipe.sadd(FAM + family, digest)
        pipe.expire(FAM + family, self.ttl_s)
        await pipe.execute()
        return token

    async def rotate(self, old_token: str) -> Rotation:
        new_token = secrets.token_urlsafe(32)
        raw = await self._rotate(
            keys=[RT + self.digest(old_token)],
            args=[
                repr(self.clock()),
                self.grace_s,
                self.digest(new_token),
                self.ttl_s,
                RT,
                FAM,
            ],
        )
        result = [_text(x) for x in raw]
        if result[0] == "ok":
            return Rotation("ok", uuid.UUID(result[1]), new_token)
        if result[0] == "reused":
            return Rotation("reused", uuid.UUID(result[1]))
        return Rotation("missing")

    async def revoke(self, token: str) -> None:
        """End the whole family the token belongs to (logout)."""
        family = await self.redis.hget(RT + self.digest(token), "fid")
        if family is None:
            return
        members = await self.redis.smembers(FAM + _text(family))
        keys = [RT + _text(m) for m in members] + [FAM + _text(family)]
        await self.redis.delete(*keys)
