"""Окружение Alembic: асинхронный движок asyncpg, настройки из config.attributes."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from antec_platform.db import metadata
from antec_platform.settings import DatabaseSettings

config = context.config


def _run(connection: Connection, settings: DatabaseSettings) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations(app_role=settings.app_role)


async def _main() -> None:
    settings: DatabaseSettings = config.attributes["settings"]
    engine = create_async_engine(settings.url(), poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run, settings)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline-режим миграций не поддерживается")
asyncio.run(_main())
