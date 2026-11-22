"""Миграции Alembic без alembic.ini: настройки БД из DatabaseSettings (ADR-0006, п. 9).

Выполняются ролью владельца схемы (`platform`) одноразовым сервисом `platform-migrate`.
"""

from __future__ import annotations

from importlib.resources import files

from alembic import command
from alembic.config import Config

from antec_platform.settings import DatabaseSettings


def alembic_config(settings: DatabaseSettings) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("antec_platform") / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def upgrade(settings: DatabaseSettings, revision: str = "head") -> None:
    command.upgrade(alembic_config(settings), revision)
