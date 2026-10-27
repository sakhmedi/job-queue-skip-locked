"""Окружение Alembic для async-движка (asyncpg)."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Произвольная константа для pg_advisory_xact_lock: если стартуют сразу несколько
# экземпляров api, миграции выполнит первый, остальные подождут и увидят, что всё применено.
MIGRATION_LOCK_ID = 7_340_001


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` — только печать SQL, без подключения к БД."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    # engine.begin() — одна транзакция на всё: блокировка снимается при её коммите.
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID}
        )
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
