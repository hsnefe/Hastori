"""Login attempt limiter: failed attempts per e-mail address and client address in a window."""

import hashlib

from redis.asyncio import Redis

MAX_FAILURES = 5
# Failures from one address with any e-mail (credential stuffing across accounts).
MAX_FAILURES_PER_IP = 30
WINDOW_S = 300


class LoginLimiter:
    def __init__(
        self, redis: Redis, max_failures: int = MAX_FAILURES, window_s: int = WINDOW_S
    ) -> None:
        self.redis = redis
        self.max_failures = max_failures
        self.window_s = window_s

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"rl:loginip:{ip}"

    @staticmethod
    def _key(email: str, ip: str) -> str:
        digest = hashlib.sha256(email.encode()).hexdigest()[:24]
        return f"rl:login:{digest}:{ip}"

    async def blocked_for(self, email: str, ip: str) -> int:
        """Seconds until another attempt is allowed; 0 if one is allowed now."""
        waits = [0]
        for key, limit in (
            (self._key(email, ip), self.max_failures),
            (self._ip_key(ip), MAX_FAILURES_PER_IP),
        ):
            count = await self.redis.get(key)
            if count is not None and int(count) >= limit:
                waits.append(max(1, int(await self.redis.ttl(key))))
        return max(waits)

    async def failed(self, email: str, ip: str) -> None:
        pipe = self.redis.pipeline()
        for key in (self._key(email, ip), self._ip_key(ip)):
            pipe.set(key, 0, ex=self.window_s, nx=True)  # the window starts at the first failure
            pipe.incr(key)
        await pipe.execute()

    async def succeeded(self, email: str, ip: str) -> None:
        await self.redis.delete(self._key(email, ip))
