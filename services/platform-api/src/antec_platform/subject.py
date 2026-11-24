"""Права субъекта данных (ADR-0012, п. 7): выгрузка и удаление, одноразовые команды оператора.

Удаление: тексты сообщений, состояние запусков, имя, email, идентичности, связь с чатами клиента,
чаты Open WebUI. Остаются псевдонимные метаданные (UUID) и аудит до своего срока. Основания и
порядок ответа: ADR-0015. Каждая команда пишет событие аудита до выдачи результата.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, delete, func, insert, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from antec_platform.adapters import open_webui
from antec_platform.db import (
    agent_runs,
    audit_events,
    conversations,
    identities,
    messages,
    model_calls,
    users,
)
from antec_platform.store import audit_row

ACTOR = "platform-subject"


class SubjectError(Exception):
    """Команду нельзя выполнить; текст для оператора."""


async def find(engine: AsyncEngine, email: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(users.c.id, users.c.display_name, users.c.email, users.c.status).where(
                func.lower(users.c.email) == email.strip().lower()
            )
        )
        return [dict(r._mapping) for r in rows]


async def _open_webui_user(
    engine: AsyncEngine,
    open_webui_engine: AsyncEngine | None,
    user_id: uuid.UUID,
    antec_uid: str | None,
) -> str | None:
    """Аккаунт Open WebUI: по oid Entra из идентичности или по явному antec_uid оператора.

    Идентификаторы чатов из запросов не используются: их утверждает клиент.
    """
    if open_webui_engine is None:
        return None
    if antec_uid is None:
        async with engine.connect() as conn:
            found_oids = list(
                (
                    await conn.execute(
                        select(identities.c.entra_oid).where(identities.c.user_id == user_id)
                    )
                ).scalars()
            )
        if not found_oids:
            # Идентичностей нет только после erase: чаты уже удалены вместе с ними.
            return None
        oids = [oid for oid in found_oids if oid is not None]
        if len(oids) != 1:
            raise SubjectError(
                "у пользователя нет единственного oid Entra: укажите --open-webui-uid "
                "(логин локального пользователя) или --skip-open-webui"
            )
        antec_uid = str(oids[0])
    found = await open_webui.find_user(open_webui_engine, antec_uid)
    if found is None:
        raise SubjectError(
            "аккаунта Open WebUI с этим antec_uid нет: проверьте --open-webui-uid или "
            "укажите --skip-open-webui"
        )
    return found


async def _require_user(conn: AsyncConnection, user_id: uuid.UUID) -> None:
    if (
        await conn.execute(select(users.c.id).where(users.c.id == user_id))
    ).scalar_one_or_none() is None:
        raise SubjectError(f"пользователя {user_id} нет")


def _event(
    action: str, user_id: uuid.UUID, details: dict[str, Any], outcome: str = "allow"
) -> dict[str, Any]:
    return audit_row(
        action=action,
        outcome=outcome,
        request_id=uuid.uuid7(),
        actor_user_id=None,
        actor_client=ACTOR,
        resource_type="user",
        resource_id=str(user_id),
        details=details,
    )


async def export(
    engine: AsyncEngine,
    user_id: uuid.UUID,
    *,
    open_webui_engine: AsyncEngine | None = None,
    antec_uid: str | None = None,
) -> dict[str, Any]:
    """Всё о субъекте одним объектом JSON."""
    async with engine.connect() as conn:
        await _require_user(conn, user_id)
    account = await _open_webui_user(engine, open_webui_engine, user_id, antec_uid)
    own = select(conversations.c.id).where(conversations.c.user_id == user_id)
    queries: dict[str, Select[Any]] = {
        "user": select(users).where(users.c.id == user_id),
        "identities": select(identities).where(identities.c.user_id == user_id),
        "conversations": select(conversations).where(conversations.c.user_id == user_id),
        "messages": select(messages)
        .where(messages.c.conversation_id.in_(own))
        .order_by(messages.c.created_at, messages.c.position),
        "model_calls": select(model_calls).where(model_calls.c.user_id == user_id),
        "agent_runs": select(agent_runs).where(agent_runs.c.user_id == user_id),
        "audit_events": select(audit_events)
        .where(
            or_(
                audit_events.c.actor_user_id == user_id,
                (audit_events.c.resource_type == "user")
                & (audit_events.c.resource_id == str(user_id)),
            )
        )
        .order_by(audit_events.c.occurred_at),
    }
    out: dict[str, Any] = {}
    # Один снимок данных на всю выгрузку.
    snapshot = engine.execution_options(isolation_level="REPEATABLE READ")
    async with snapshot.connect() as conn:
        for name, stmt in queries.items():
            out[name] = [dict(r._mapping) for r in await conn.execute(stmt)]
    if open_webui_engine is not None and account is not None:
        out["open_webui_chats"] = await open_webui.export_chats(open_webui_engine, account)
    async with engine.begin() as conn:
        await conn.execute(
            insert(audit_events).values(
                _event("subject.export", user_id, {k: len(v) for k, v in out.items()})
            )
        )
    return out


async def erase(
    engine: AsyncEngine,
    user_id: uuid.UUID,
    *,
    open_webui_engine: AsyncEngine | None = None,
    antec_uid: str | None = None,
) -> dict[str, int]:
    """Удаление; повторный запуск безопасен (в том числе после восстановления из бэкапа)."""
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        await _require_user(conn, user_id)
    # Сначала чаты: после удаления идентичностей аккаунт уже не найти.
    account = await _open_webui_user(engine, open_webui_engine, user_id, antec_uid)
    if open_webui_engine is not None and account is not None:
        counts["open_webui_chats"] = await open_webui.erase_chats(open_webui_engine, account)
    own = select(conversations.c.id).where(conversations.c.user_id == user_id)
    async with engine.begin() as conn:
        # Отключение и удаление идентичностей раньше текстов: новые запросы с ними не пройдут.
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(display_name=None, email=None, status="disabled")
        )
        for name, stmt in (
            ("identities", delete(identities).where(identities.c.user_id == user_id)),
            ("messages", delete(messages).where(messages.c.conversation_id.in_(own))),
            (
                "conversations",
                update(conversations)
                .where(conversations.c.user_id == user_id, conversations.c.external_id.is_not(None))
                .values(external_id=None),
            ),
            (
                "agent_runs",
                update(agent_runs)
                .where(
                    agent_runs.c.user_id == user_id,
                    agent_runs.c.status == "awaiting_approval",
                )
                .values(status="expired", state=null(), pending=null()),
            ),
        ):
            counts[name] = (await conn.execute(stmt)).rowcount
        await conn.execute(
            insert(audit_events).values(_event("subject.erase", user_id, dict(counts)))
        )
    return counts
