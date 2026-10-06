import asyncio
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from hastori_common.models import Base
from hastori_common.settings import get_settings

config = context.config
config.set_main_option(
    "sqlalchemy.url", get_settings(strict=("database_url",)).database_url.replace("%", "%%")
)
target_metadata = Base.metadata

_TIMESCALE_SCHEMAS = {
    "_timescaledb_catalog",
    "_timescaledb_internal",
    "_timescaledb_cache",
    "_timescaledb_config",
    "timescaledb_information",
    "timescaledb_experimental",
}
_MANAGED_BY_SQL = {"measurements", "measurements_1m"}


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if getattr(obj, "schema", None) in _TIMESCALE_SCHEMAS:
        return False
    return not (type_ == "table" and name in _MANAGED_BY_SQL)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        # Each revision commits (and is stamped) on its own: if a later one fails, the earlier
        # ones stay applied and a rerun resumes instead of colliding with half-created objects.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


asyncio.run(_main())
