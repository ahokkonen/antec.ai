"""Класс данных разговора (ADR-0011): наибольший класс данных, попавших в разговор.

Повышается каждым разрешённым вызовом инструмента с классом выше класса по умолчанию; модель
каждого следующего запроса в разговоре должна быть допущена к нему. Пусто = класс по умолчанию.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

_CHECK = "data_class in ('public', 'internal', 'confidential', 'restricted')"


def upgrade(*, app_role: str) -> None:
    op.add_column("conversations", sa.Column("data_class", sa.Text()))
    op.create_check_constraint("ck_conversations_data_class", "conversations", _CHECK)


def downgrade(*, app_role: str) -> None:
    op.drop_constraint("ck_conversations_data_class", "conversations", type_="check")
    op.drop_column("conversations", "data_class")
