"""Миграции Alembic без alembic.ini: настройки БД из DatabaseSettings (ADR-0006, п. 9).

Выполняются ролью владельца схемы (`platform`) одноразовым сервисом `platform-migrate`.
`plan` сообщает `up.sh`, что будет применено (ADR-0014, п. 7): бэкап перед любой ожидающей
миграцией и отказ, если БД на ревизии, которой нет в коде (понижение версии ядра).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from antec_platform.settings import DatabaseSettings


def alembic_config(settings: DatabaseSettings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("antec_platform") / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def upgrade(settings: DatabaseSettings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)


@dataclass(frozen=True)
class MigrationPlan:
    """current: ревизия БД (None = пустая БД); known: ревизия есть в коде этой версии."""

    current: str | None
    head: str
    known: bool
    pending: list[str]
    irreversible: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def _current_revision(settings: DatabaseSettings) -> str | None:
    engine = create_async_engine(settings.url(), poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_current)
    finally:
        await engine.dispose()


def build_plan(script: ScriptDirectory, current: str | None) -> MigrationPlan:
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("в коде нет миграций")
    if current is not None:
        try:
            known = script.get_revision(current) is not None
        except CommandError:
            known = False
        if not known:
            return MigrationPlan(current, head, False, [], [])
    # iterate_revisions(head, current) идёт от головы вниз и не включает current.
    revisions = list(script.iterate_revisions(head, current or "base"))[::-1]
    pending = [r.revision for r in revisions]
    # Без явного флага миграция считается необратимой: ошибка в безопасную сторону.
    irreversible = [r.revision for r in revisions if getattr(r.module, "IRREVERSIBLE", True)]
    return MigrationPlan(current, head, True, pending, irreversible)


def plan(settings: DatabaseSettings) -> MigrationPlan:
    script = ScriptDirectory.from_config(alembic_config(settings))
    return build_plan(script, asyncio.run(_current_revision(settings)))
