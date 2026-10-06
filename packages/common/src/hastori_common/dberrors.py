"""Which database errors are worth retrying.

Only SQLSTATE class 22 (data exception) and class 23 (integrity violation) say "this row is the
problem". Everything else the database can raise (connection loss, restart, failover, a missing
table after a bad migration, a wrong password) belongs to the environment: retry it, never drop
the work, or an outage turns into silent data loss.
"""

import asyncpg

PERMANENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.exceptions.DataError,
    asyncpg.exceptions.IntegrityConstraintViolationError,
)
RETRYABLE_DB_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    asyncpg.InterfaceError,
    asyncpg.PostgresError,
)
