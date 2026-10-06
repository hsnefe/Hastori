"""Who is asking and which sites they may see. Built from the database on every request, never
from the token: a changed role or site list applies immediately, not when the token expires."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.errors import forbidden, not_found

SYSTEM_ADMIN = "system_admin"
SITE_ADMIN = "site_admin"
VIEWER = "viewer"
WRITERS = (SYSTEM_ADMIN, SITE_ADMIN)

# One query: the user and every site of their organisation they may see (all of them for a
# system admin, the assigned ones otherwise).
SCOPE_SQL = text(
    """
    SELECT u.id, u.org_id, u.email, u.role,
           COALESCE(array_agg(s.id) FILTER (WHERE s.id IS NOT NULL), '{}') AS site_ids
    FROM users u
    LEFT JOIN sites s
      ON s.org_id = u.org_id
     AND (u.role = 'system_admin'
          OR EXISTS (SELECT 1 FROM user_sites us WHERE us.user_id = u.id AND us.site_id = s.id))
    WHERE u.id = :user_id
    GROUP BY u.id
    """
)


@dataclass(frozen=True)
class SiteScope:
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    site_ids: frozenset[uuid.UUID]

    @property
    def can_write(self) -> bool:
        return self.role in WRITERS

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise forbidden()

    def require_site(self, site_id: uuid.UUID) -> None:
        """A site outside the scope does not exist as far as this user can tell: 404, not 403,
        so the answer does not confirm that the id belongs to somebody else's site."""
        if site_id not in self.site_ids:
            raise not_found("Site not found")


async def load_scope(session: AsyncSession, user_id: uuid.UUID) -> SiteScope | None:
    row = (await session.execute(SCOPE_SQL, {"user_id": user_id})).mappings().first()
    if row is None:
        return None
    return SiteScope(
        user_id=row["id"],
        org_id=row["org_id"],
        email=row["email"],
        role=row["role"],
        site_ids=frozenset(row["site_ids"]),
    )
