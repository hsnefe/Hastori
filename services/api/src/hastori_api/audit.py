"""The one place that writes the audit log. Every change of rules, users and sites and every
acknowledgement goes through here, inside the transaction of the change itself, so a change and
its record commit together or not at all."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.scope import SiteScope
from hastori_common.models import AuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def audit(
    session: AsyncSession,
    scope: SiteScope,
    action: str,
    entity: str,
    entity_id: object,
    detail: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the session (written with the surrounding commit). Never put secrets
    in `detail`: a password change records that it happened, not the value."""
    session.add(
        AuditLog(
            user_id=scope.user_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            detail=_jsonable(detail) if detail else None,
        )
    )
