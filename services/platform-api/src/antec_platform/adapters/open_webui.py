"""Чаты Open WebUI 0.13.3 в его БД (ADR-0012, пп. 1 и 7). Единственное место с его схемой.

Open WebUI не умеет удалять чаты по сроку, а его API удаления требует сессии владельца чата.
Порядок и таблицы повторяют `Chats.delete_chat_by_id` (backend/open_webui/models/chats.py):
chat_message, chat_file и shared_chat удаляет каскад, остальное ссылается на чат без внешнего
ключа. Версия Open WebUI в реестре меняется вместе с проверкой этого файла; права ролей:
deploy/compose/open-webui/retention-grants.sql.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_BATCH = 200
# Чат с непрерывной перепиской не живёт дольше верхней границы срока содержимого.
_MAX_AGE = dt.timedelta(days=180)

_EXPIRED = text(
    "delete from chat where id in (select id from chat where updated_at < :updated "
    "or created_at < :created limit :n) and (updated_at < :updated or created_at < :created) "
    "returning id"
)
_OF_USER = text("delete from chat where user_id = :user_id returning id")
_CLEANUP: tuple[TextClause, ...] = tuple(
    text(sql)
    for sql in (
        "update automation_run set chat_id = null where chat_id = any(:ids)",
        "delete from comment_reaction where comment_id in (select id from comment "
        "where resource_type = 'chat' and resource_id = any(:ids))",
        "delete from comment where resource_type = 'chat' and resource_id = any(:ids)",
        "delete from notification where resource_type = 'chat' and resource_id = any(:ids)",
        "delete from chat_view_state where chat_id = any(:ids)",
        "delete from chatidtag where chat_id = any(:ids)",
    )
)


async def _cleanup(conn: AsyncConnection, ids: list[str]) -> None:
    if ids:
        for stmt in _CLEANUP:
            await conn.execute(stmt, {"ids": ids})


async def purge_chats(engine: AsyncEngine, content_days: int) -> int:
    """Чаты без активности дольше срока содержимого, а также старше верхней границы срока."""
    now = dt.datetime.now(dt.UTC)
    params = {
        "updated": int((now - dt.timedelta(days=content_days)).timestamp()),
        "created": int((now - _MAX_AGE).timestamp()),
        "n": _BATCH,
    }
    total = 0
    while True:
        async with engine.begin() as conn:
            ids = list((await conn.execute(_EXPIRED, params)).scalars())
            await _cleanup(conn, ids)
        total += len(ids)
        if len(ids) < _BATCH:
            return total


async def find_user(engine: AsyncEngine, antec_uid: str) -> str | None:
    """Аккаунт по antec_uid (OAUTH_SUB_CLAIM): oid Entra или логин локального пользователя."""
    async with engine.connect() as conn:
        rows = list(
            (
                await conn.execute(
                    text("select id from \"user\" where oauth -> 'oidc' ->> 'sub' = :uid"),
                    {"uid": antec_uid},
                )
            ).scalars()
        )
    if len(rows) > 1:
        raise RuntimeError(f"в Open WebUI несколько аккаунтов с antec_uid {antec_uid}")
    return str(rows[0]) if rows else None


async def export_chats(engine: AsyncEngine, owui_user_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "select id, title, created_at, updated_at, chat, summary from chat "
                "where user_id = :u order by created_at"
            ),
            {"u": owui_user_id},
        )
        return [dict(r._mapping) for r in rows]


async def erase_chats(engine: AsyncEngine, owui_user_id: str) -> int:
    async with engine.begin() as conn:
        ids = list((await conn.execute(_OF_USER, {"user_id": owui_user_id})).scalars())
        await _cleanup(conn, ids)
    return len(ids)
