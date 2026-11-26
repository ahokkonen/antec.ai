"""Подтверждения инструментов агентов (ADR-0010, п. 7).

agent_runs: решения по ожиданию (approved, denied, expired), код и срок подтверждения,
ожидающие вызовы (без результатов), ссылка продолжения на исходный запуск.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None
IRREVERSIBLE = False  # есть downgrade (ADR-0014, п. 3)

_OLD = "status in ('started', 'ok', 'error', 'cancelled', 'rejected', 'awaiting_approval')"
_NEW = (
    "status in ('started', 'ok', 'error', 'cancelled', 'rejected', 'awaiting_approval', "
    "'approved', 'denied', 'expired')"
)


def _uuid() -> UUID[uuid.UUID]:
    return UUID(as_uuid=True)


def upgrade(*, app_role: str) -> None:
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", _NEW)
    op.add_column("agent_runs", sa.Column("pending", JSONB()))
    op.add_column("agent_runs", sa.Column("approval_code", sa.Text()))
    op.add_column("agent_runs", sa.Column("approval_expires_at", sa.DateTime(timezone=True)))
    op.add_column("agent_runs", sa.Column("parent_run_id", _uuid()))
    op.create_foreign_key(
        "fk_agent_runs_parent_run_id_agent_runs",
        "agent_runs",
        "agent_runs",
        ["parent_run_id"],
        ["id"],
    )
    op.create_index(
        "ix_agent_runs_conversation_id_status", "agent_runs", ["conversation_id", "status"]
    )


def downgrade(*, app_role: str) -> None:
    op.drop_index("ix_agent_runs_conversation_id_status", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_parent_run_id_agent_runs", "agent_runs", type_="foreignkey")
    for column in ("parent_run_id", "approval_expires_at", "approval_code", "pending"):
        op.drop_column("agent_runs", column)
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint("ck_agent_runs_status", "agent_runs", _OLD)
