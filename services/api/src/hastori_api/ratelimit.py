"""Login attempt limiter: failed attempts per e-mail address and client address in a window."""

import hashlib

from redis.asyncio import Redis

MAX_FAILURES = 5
WINDOW_S = 300


class LoginLimiter:
    def __init__(
        self, redis: Redis, max_failures: int = MAX_FAILURES, window_s: int = WINDOW_S
    ) -> None:
        self.redis = redis
        self.max_failures = max_failures
        self.window_s = window_s

    @staticmethod
    def _key(email: str, ip: str) -> str:
        digest = hashlib.sha256(email.encode()).hexdigest()[:24]
        return f"rl:login:{digest}:{ip}"

    async def blocked_for(self, email: str, ip: str) -> int:
        """Seconds until another attempt is allowed; 0 if one is allowed now."""
        key = self._key(email, ip)
        count = await self.redis.get(key)
        if count is None or int(count) < self.max_failures:
            return 0
        return max(1, int(await self.redis.ttl(key)))

    async def failed(self, email: str, ip: str) -> None:
        key = self._key(email, ip)
        pipe = self.redis.pipeline()
        pipe.set(key, 0, ex=self.window_s, nx=True)  # the window starts at the first failure
        pipe.incr(key)
        await pipe.execute()

    async def succeeded(self, email: str, ip: str) -> None:
        await self.redis.delete(self._key(email, ip))
