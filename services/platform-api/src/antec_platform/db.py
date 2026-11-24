"""Схема БД Platform API (ADR-0006, пп. 4-9), SQLAlchemy Core.

Схема меняется только миграциями Alembic (`antec_platform/migrations`); эти определения
используются запросами и проверяются тестом на совпадение с миграциями.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from antec_platform.settings import DatabaseSettings

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

NOW = text("now()")

users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("display_name", Text),
    Column("email", Text),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("status in ('active', 'disabled')", name="status"),
)

identities = Table(
    "identities",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("idp", Text),
    Column("entra_tid", Text),
    Column("entra_oid", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    UniqueConstraint("issuer", "subject"),
    UniqueConstraint("entra_tid", "entra_oid"),
    Index("ix_identities_user_id", "user_id"),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("client_id", Text, nullable=False),
    Column("external_id", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    # Наибольший класс данных в разговоре (ADR-0011); пусто = класс по умолчанию.
    Column("data_class", Text),
    CheckConstraint(
        "data_class in ('public', 'internal', 'confidential', 'restricted')", name="data_class"
    ),
    UniqueConstraint("user_id", "client_id", "external_id"),
    Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),
    Index("ix_conversations_updated_at", "updated_at"),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False),
    Column("client_id", Text, nullable=False),
    Column("agent", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("stream", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("steps", Integer, nullable=False, server_default=text("0")),
    Column("prompt_tokens", BigInteger),
    Column("completion_tokens", BigInteger),
    Column("error", Text),
    Column("state", JSONB),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("finished_at", DateTime(timezone=True)),
    # Подтверждение (ADR-0010, п. 7): ожидающие вызовы, код, срок, продолжение.
    Column("pending", JSONB),
    Column("approval_code", Text),
    Column("approval_expires_at", DateTime(timezone=True)),
    Column("parent_run_id", UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")),
    CheckConstraint(
        "status in ('started', 'ok', 'error', 'cancelled', 'rejected', 'awaiting_approval', "
        "'approved', 'denied', 'expired')",
        name="status",
    ),
    Index("ix_agent_runs_user_id_started_at", "user_id", "started_at"),
    Index("ix_agent_runs_conversation_id", "conversation_id"),
    Index("ix_agent_runs_conversation_id_status", "conversation_id", "status"),
    Index("ix_agent_runs_started_at", "started_at"),
    Index("ix_agent_runs_parent_run_id", "parent_run_id"),
    # Незавершённые запуски для platform-worker (ADR-0012, п. 2).
    Index(
        "ix_agent_runs_open",
        "started_at",
        postgresql_where=text("status in ('started', 'awaiting_approval')"),
    ),
)

model_calls = Table(
    "model_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False),
    Column("client_id", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("stream", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("http_status", Integer),
    Column("prompt_tokens", BigInteger),
    Column("completion_tokens", BigInteger),
    Column("cost_usd", Numeric(18, 8)),
    Column("gateway_request_id", Text),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("finished_at", DateTime(timezone=True)),
    # Шаг запуска агента (ADR-0010, п. 5); пусто у прямых вызовов модели.
    Column("agent_run_id", UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")),
    CheckConstraint("status in ('started', 'ok', 'error', 'cancelled', 'rejected')", name="status"),
    Index("ix_model_calls_user_id_started_at", "user_id", "started_at"),
    Index("ix_model_calls_conversation_id", "conversation_id"),
    Index("ix_model_calls_agent_run_id", "agent_run_id"),
    Index("ix_model_calls_started_at", "started_at"),
    Index("ix_model_calls_open", "started_at", postgresql_where=text("status = 'started'")),
)

messages = Table(
    "messages",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False),
    Column("model_call_id", UUID(as_uuid=True), ForeignKey("model_calls.id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("role", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("role in ('system', 'developer', 'user', 'assistant', 'tool')", name="role"),
    UniqueConstraint("model_call_id", "position"),
    Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    # Очистка по сроку (ADR-0012, п. 1).
    Index("ix_messages_created_at", "created_at"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("actor_user_id", UUID(as_uuid=True)),
    Column("actor_client", Text),
    Column("action", Text, nullable=False),
    Column("resource_type", Text),
    Column("resource_id", Text),
    Column("outcome", Text, nullable=False),
    Column("request_id", UUID(as_uuid=True)),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # Один вызов инструмента: tool.call.started до него и tool.call после (ADR-0012, п. 3).
    Column("call_id", UUID(as_uuid=True)),
    CheckConstraint("outcome in ('allow', 'deny', 'error')", name="outcome"),
    Index("ix_audit_events_occurred_at", "occurred_at"),
    Index(
        "uq_audit_events_call_id_action",
        "call_id",
        "action",
        unique=True,
        postgresql_where=text("call_id is not null"),
    ),
    Index(
        "ix_audit_events_tool_started",
        "occurred_at",
        postgresql_where=text("action = 'tool.call.started'"),
    ),
    Index("ix_audit_events_actor_user_id_occurred_at", "actor_user_id", "occurred_at"),
)


def create_engine(
    settings: DatabaseSettings, application_name: str = "platform-api", database: str | None = None
) -> AsyncEngine:
    return create_async_engine(
        settings.url() if database is None else settings.url().set(database=database),
        pool_size=settings.database_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=1800,
        # Параметры запросов (тексты сообщений) не попадают в тексты ошибок (ADR-0012, п. 5).
        hide_parameters=True,
        connect_args={"server_settings": {"application_name": application_name}},
    )
