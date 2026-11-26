"""Начальная схема: пользователи, идентичности, разговоры, вызовы моделей, сообщения, аудит.

ADR-0006, пп. 4-9. Роль сервиса (`app_role`) получает только DML без DELETE; аудит только на
добавление (права и триггер).

Revision ID: 0001
Revises:
Create Date: 2026-11-22
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None
# Необратимая миграция: up.sh делает бэкап, откат только восстановлением (ADR-0014, п. 3).
IRREVERSIBLE = True  # откат бросает исключение

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_WORK_TABLES = ("users", "identities", "conversations", "model_calls", "messages")


def _uuid() -> UUID[uuid.UUID]:
    return UUID(as_uuid=True)


def upgrade(*, app_role: str) -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("select 1 from pg_roles where rolname = :r"), {"r": app_role}
    ).scalar()
    if not exists:
        raise RuntimeError(
            f"роли {app_role} нет: её создаёт scripts/up.sh до миграций (ADR-0006, п. 9)"
        )

    op.create_table(
        "users",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("last_seen_at", _TS, server_default=_NOW, nullable=False),
        sa.CheckConstraint("status in ('active', 'disabled')", name="ck_users_status"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "identities",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("idp", sa.Text()),
        sa.Column("entra_tid", sa.Text()),
        sa.Column("entra_oid", sa.Text()),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("last_seen_at", _TS, server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_identities_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_identities"),
        sa.UniqueConstraint("issuer", "subject", name="uq_identities_issuer_subject"),
        sa.UniqueConstraint("entra_tid", "entra_oid", name="uq_identities_entra_tid_entra_oid"),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])
    op.create_table(
        "conversations",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text()),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TS, server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_conversations_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint(
            "user_id",
            "client_id",
            "external_id",
            name="uq_conversations_user_id_client_id_external_id",
        ),
    )
    op.create_index(
        "ix_conversations_user_id_updated_at", "conversations", ["user_id", "updated_at"]
    )
    op.create_table(
        "model_calls",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("user_id", _uuid(), nullable=False),
        sa.Column("conversation_id", _uuid(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("prompt_tokens", sa.BigInteger()),
        sa.Column("completion_tokens", sa.BigInteger()),
        sa.Column("cost_usd", sa.Numeric(18, 8)),
        sa.Column("gateway_request_id", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("finished_at", _TS),
        sa.CheckConstraint(
            "status in ('started', 'ok', 'error', 'cancelled', 'rejected')",
            name="ck_model_calls_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_model_calls_user_id_users"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_model_calls_conversation_id_conversations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_calls"),
    )
    op.create_index("ix_model_calls_user_id_started_at", "model_calls", ["user_id", "started_at"])
    op.create_index("ix_model_calls_conversation_id", "model_calls", ["conversation_id"])
    op.create_table(
        "messages",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("conversation_id", _uuid(), nullable=False),
        sa.Column("model_call_id", _uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "role in ('system', 'developer', 'user', 'assistant', 'tool')", name="ck_messages_role"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id_conversations",
        ),
        sa.ForeignKeyConstraint(
            ["model_call_id"], ["model_calls.id"], name="fk_messages_model_call_id_model_calls"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("model_call_id", "position", name="uq_messages_model_call_id_position"),
    )
    op.create_index(
        "ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"]
    )
    op.create_table(
        "audit_events",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("occurred_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("actor_user_id", _uuid()),
        sa.Column("actor_client", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text()),
        sa.Column("resource_id", sa.Text()),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("request_id", _uuid()),
        sa.Column("details", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("outcome in ('allow', 'deny', 'error')", name="ck_audit_events_outcome"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index(
        "ix_audit_events_actor_user_id_occurred_at",
        "audit_events",
        ["actor_user_id", "occurred_at"],
    )

    # Аудит только на добавление: триггер срабатывает и для владельца схемы (ADR-0006, п. 8).
    op.execute(
        """
        create function audit_events_append_only() returns trigger
        language plpgsql as $$
        begin
          raise exception 'audit_events: только добавление (ADR-0006)'
            using errcode = 'insufficient_privilege';
        end
        $$
        """
    )
    op.execute(
        "create trigger audit_events_no_update_delete before update or delete on audit_events "
        "for each row execute function audit_events_append_only()"
    )
    op.execute(
        "create trigger audit_events_no_truncate before truncate on audit_events "
        "for each statement execute function audit_events_append_only()"
    )

    role = conn.dialect.identifier_preparer.quote_identifier(app_role)
    op.execute(f"grant usage on schema public to {role}")
    op.execute(f"grant select, insert, update on {', '.join(_WORK_TABLES)} to {role}")
    op.execute(f"grant select, insert on audit_events to {role}")


def downgrade(*, app_role: str) -> None:
    raise RuntimeError("откат начальной схемы не поддерживается: данные аудита не удаляются")
