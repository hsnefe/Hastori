"""Time handling shared by the routers."""

from datetime import UTC, datetime

from hastori_api.errors import ApiError


def as_utc(value: datetime) -> datetime:
    """A time without an offset is taken as UTC. A year-1 date with an offset converts to a
    time before year 1: that is a bad request, not a server error."""
    try:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ApiError(422, "Time out of range", code="validation_error") from None
