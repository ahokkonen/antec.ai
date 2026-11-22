"""Запуски агентов (ADR-0010, п. 5): agent_runs и ссылка шагов в model_calls.

`state` (JSONB) зарезервирован под внутреннее состояние рантайма для продолжения после
подтверждения: его можно отбросить при смене рантайма (ADR-0010, п. 1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")


def _uuid() -> UUID[uuid.UUID]:
    return UUID(as_uuid=True)


def upgrade(*, app_role: str) -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("conversation_id", _uuid(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger()),
        sa.Column("completion_tokens", sa.BigInteger()),
        sa.Column("error", sa.Text()),
        sa.Column("state", JSONB()),
        sa.Column("started_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("finished_at", _TS),
        sa.CheckConstraint(
            "status in ('started', 'ok', 'error', 'cancelled', 'rejected', 'awaiting_approval')",
            name="ck_agent_runs_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agent_runs_user_id_users"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_agent_runs_conversation_id_conversations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index("ix_agent_runs_user_id_started_at", "agent_runs", ["user_id", "started_at"])
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.add_column("model_calls", sa.Column("agent_run_id", _uuid()))
    op.create_foreign_key(
        "fk_model_calls_agent_run_id_agent_runs",
        "model_calls",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
    )
    op.create_index("ix_model_calls_agent_run_id", "model_calls", ["agent_run_id"])

    role = op.get_bind().dialect.identifier_preparer.quote_identifier(app_role)
    op.execute(f"grant select, insert, update on agent_runs to {role}")


def downgrade(*, app_role: str) -> None:
    op.drop_index("ix_model_calls_agent_run_id", table_name="model_calls")
    op.drop_constraint("fk_model_calls_agent_run_id_agent_runs", "model_calls", type_="foreignkey")
    op.drop_column("model_calls", "agent_run_id")
    op.drop_table("agent_runs")
