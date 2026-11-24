"""Сроки хранения и права субъекта данных (ADR-0012, пп. 1, 2, 7).

Индексы очистки, ссылки на запуски ON DELETE SET NULL, права ролей worker_role и subject_role.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_RUN_LINKS = (
    ("agent_runs", "fk_agent_runs_parent_run_id_agent_runs", "parent_run_id"),
    ("model_calls", "fk_model_calls_agent_run_id_agent_runs", "agent_run_id"),
)
_INDEXES = (
    ("ix_messages_created_at", "messages", "created_at"),
    ("ix_model_calls_started_at", "model_calls", "started_at"),
    ("ix_agent_runs_started_at", "agent_runs", "started_at"),
    ("ix_agent_runs_parent_run_id", "agent_runs", "parent_run_id"),
    ("ix_conversations_updated_at", "conversations", "updated_at"),
)


def upgrade(*, app_role: str) -> None:
    conn = op.get_bind()
    settings = context.config.attributes["settings"]
    for role in (settings.worker_role, settings.subject_role):
        if not conn.execute(
            sa.text("select 1 from pg_roles where rolname = :r"), {"r": role}
        ).scalar():
            raise RuntimeError(f"роли {role} нет: её создаёт scripts/up.sh до миграций")

    for table, name, column in _RUN_LINKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "agent_runs", [column], ["id"], ondelete="SET NULL")
    for name, table, column in _INDEXES:
        op.create_index(name, table, [column])

    quote = conn.dialect.identifier_preparer.quote_identifier
    worker, subject = quote(settings.worker_role), quote(settings.subject_role)
    # Очистка: тексты и состояние по-прежнему не читаются.
    op.execute(
        f"grant select (id, model_call_id, conversation_id, created_at) on messages to {worker}"
    )
    op.execute(f"grant select (agent_run_id) on model_calls to {worker}")
    op.execute(f"grant select (id, updated_at) on conversations to {worker}")
    op.execute(f"grant delete on messages, model_calls, agent_runs, conversations to {worker}")

    op.execute(f"grant usage on schema public to {subject}")
    op.execute(
        "grant select on users, identities, conversations, messages, model_calls, agent_runs, "
        f"audit_events to {subject}"
    )
    op.execute(f"grant insert on audit_events to {subject}")
    op.execute(f"grant delete on messages, identities to {subject}")
    op.execute(f"grant update (display_name, email, status) on users to {subject}")
    op.execute(f"grant update (external_id) on conversations to {subject}")
    op.execute(f"grant update (status, state, pending) on agent_runs to {subject}")


def downgrade(*, app_role: str) -> None:
    raise RuntimeError("откат не поддерживается: данные, удалённые по сроку, не восстановить")
