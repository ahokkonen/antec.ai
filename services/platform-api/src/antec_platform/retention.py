"""Очистка по срокам хранения (ADR-0012, п. 1): суточная задача platform-worker.

Порядок по внешним ключам: сообщения, вызовы моделей, запуски агентов, пустые разговоры. Пачка =
транзакция. Аудит здесь не удаляется: только функцией владельца схемы (ADR-0012, п. 4).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ColumnElement, Table, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from antec_platform.adapters import open_webui
from antec_platform.db import agent_runs, audit_events, conversations, messages, model_calls
from antec_platform.store import audit_row

_BATCH = 1000


async def _purge(engine: AsyncEngine, table: Table, expired: ColumnElement[bool]) -> int:
    total = 0
    while True:
        async with engine.begin() as conn:
            stmt = delete(table).where(
                table.c.id.in_(select(table.c.id).where(expired).limit(_BATCH)),
                # Повторно при удалении: строку могли обновить после выбора (новый запрос).
                expired,
            )
            deleted = (await conn.execute(stmt)).rowcount
        total += deleted
        if deleted < _BATCH:
            return total


async def purge(
    engine: AsyncEngine,
    *,
    content_days: int,
    metadata_days: int,
    open_webui_engine: AsyncEngine | None = None,
) -> dict[str, int]:
    """Удаляет просроченное; событие retention.purge, если что-то удалено или был сбой."""
    content = func.now() - dt.timedelta(days=content_days)
    meta = func.now() - dt.timedelta(days=metadata_days)
    plan: list[tuple[str, Table, ColumnElement[bool]]] = [
        ("messages", messages, messages.c.created_at < content),
        (
            "model_calls",
            model_calls,
            (model_calls.c.started_at < meta)
            & ~select(messages.c.id).where(messages.c.model_call_id == model_calls.c.id).exists(),
        ),
        ("agent_runs", agent_runs, agent_runs.c.started_at < meta),
        (
            "conversations",
            conversations,
            (conversations.c.updated_at < meta)
            & ~select(messages.c.id)
            .where(messages.c.conversation_id == conversations.c.id)
            .exists()
            & ~select(model_calls.c.id)
            .where(model_calls.c.conversation_id == conversations.c.id)
            .exists()
            & ~select(agent_runs.c.id)
            .where(agent_runs.c.conversation_id == conversations.c.id)
            .exists(),
        ),
    ]
    counts: dict[str, int] = {}
    outcome = "error"
    try:
        for name, table, expired in plan:
            counts[name] = await _purge(engine, table, expired)
        if open_webui_engine is not None:
            counts["open_webui_chats"] = await open_webui.purge_chats(
                open_webui_engine, content_days
            )
        outcome = "allow"
    finally:
        if outcome == "error" or any(counts.values()):
            async with engine.begin() as conn:
                await conn.execute(
                    insert(audit_events).values(
                        audit_row(
                            action="retention.purge",
                            outcome=outcome,
                            request_id=uuid.uuid7(),
                            actor_user_id=None,
                            actor_client="platform-worker",
                            resource_type="retention",
                            details={
                                "deleted": counts,
                                "content_days": content_days,
                                "metadata_days": metadata_days,
                            },
                        )
                    )
                )
    return counts
