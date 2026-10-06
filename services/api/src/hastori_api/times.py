"""Time handling shared by the routers."""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """A time without an offset is taken as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
