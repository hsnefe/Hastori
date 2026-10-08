"""Single-use WebSocket tickets.

A browser cannot set an Authorization header on a WebSocket, and a token in the URL ends up in
proxy logs and browser history. So the client first asks (with its bearer token) for a ticket:
32 random bytes, valid for 30 seconds, usable once. Redis keeps only its SHA-256, mapped to the
user; opening the socket consumes it with GETDEL, so a replay finds nothing.
"""

import hashlib
import secrets
import uuid

from redis.asyncio import Redis

PREFIX = "hastori:wsticket:"


def _key(ticket: str) -> str:
    return PREFIX + hashlib.sha256(ticket.encode()).hexdigest()


async def issue_ticket(redis: Redis, user_id: uuid.UUID, ttl_s: int) -> str:
    ticket = secrets.token_urlsafe(32)
    await redis.set(_key(ticket), str(user_id), ex=ttl_s)
    return ticket


async def consume_ticket(redis: Redis, ticket: str) -> uuid.UUID | None:
    """The user the ticket was issued to, or None (never issued, expired or already used)."""
    value = await redis.getdel(_key(ticket))
    if value is None:
        return None
    try:
        return uuid.UUID(value.decode() if isinstance(value, bytes) else str(value))
    except ValueError:
        return None
