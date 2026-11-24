"""Запись до действия и platform-worker (ADR-0012, пп. 1-3, 5).

audit_events.call_id связывает tool.call.started с итогом; индексы незавершённого; права роли
`worker_role`. Данные: у решённых ожиданий удаляются состояние и аргументы, тексты ошибок
(могли содержать ввод пользователя) заменяются отметкой.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade(*, app_role: str) -> None:
    conn = op.get_bind()
    worker_role: str = context.config.attributes["settings"].worker_role
    if not conn.execute(
        sa.text("select 1 from pg_roles where rolname = :r"), {"r": worker_role}
    ).scalar():
        raise RuntimeError(f"роли {worker_role} нет: её создаёт scripts/up.sh до миграций")

    op.add_column("audit_events", sa.Column("call_id", UUID(as_uuid=True)))
    op.create_index(
        "uq_audit_events_call_id_action",
        "audit_events",
        ["call_id", "action"],
        unique=True,
        postgresql_where=sa.text("call_id is not null"),
    )
    op.create_index(
        "ix_audit_events_tool_started",
        "audit_events",
        ["occurred_at"],
        postgresql_where=sa.text("action = 'tool.call.started'"),
    )
    op.create_index(
        "ix_agent_runs_open",
        "agent_runs",
        ["started_at"],
        postgresql_where=sa.text("status in ('started', 'awaiting_approval')"),
    )
    op.create_index(
        "ix_model_calls_open",
        "model_calls",
        ["started_at"],
        postgresql_where=sa.text("status = 'started'"),
    )

    op.execute(
        """
        update agent_runs set
          state = null,
          pending = case when jsonb_typeof(pending) = 'array' then (
            select coalesce(jsonb_agg(e - 'arguments'), '[]'::jsonb)
            from jsonb_array_elements(pending) as e)
            else pending end
        where status <> 'awaiting_approval' and (state is not null or pending is not null)
        """
    )
    op.execute("update agent_runs set error = 'redacted' where error is not null")
    op.execute("update model_calls set error = 'redacted' where error is not null")

    role = conn.dialect.identifier_preparer.quote_identifier(worker_role)
    op.execute(f"grant usage on schema public to {role}")
    op.execute(
        "grant select (id, user_id, conversation_id, client_id, agent, model, status, pending, "
        f"started_at, approval_expires_at) on agent_runs to {role}"
    )
    op.execute(f"grant update (status, state, pending, error, finished_at) on agent_runs to {role}")
    op.execute(
        "grant select (id, user_id, conversation_id, client_id, model, status, started_at) "
        f"on model_calls to {role}"
    )
    op.execute(f"grant update (status, error, finished_at) on model_calls to {role}")
    op.execute(f"grant select, insert on audit_events to {role}")


def downgrade(*, app_role: str) -> None:
    raise RuntimeError("откат не поддерживается: связь событий аудита не удаляется")
