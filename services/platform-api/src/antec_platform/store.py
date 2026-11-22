"""Хранилище Platform API: пользователи, история, вызовы моделей, аудит (ADR-0006)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from antec_platform.auth import Principal
from antec_platform.db import audit_events, conversations, identities, messages, model_calls, users

log = logging.getLogger(__name__)

# last_seen_at обновляется не чаще, чем раз в это время: иначе каждый запрос пишет в users.
_SEEN_INTERVAL = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    status: str


@dataclass(slots=True)
class CallResult:
    """Итог вызова модели для model_calls, messages и аудита."""

    status: str
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: Decimal | None = None
    gateway_request_id: str | None = None
    error: str | None = None
    response_message: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CallStart:
    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    client_id: str
    model: str
    stream: bool
    new_messages: list[dict[str, Any]] = field(default_factory=list)


class Store(Protocol):
    async def ping(self) -> None: ...

    async def resolve_user(self, principal: Principal, request_id: uuid.UUID) -> User: ...

    async def record_denied(
        self, principal: Principal, reason: str, request_id: uuid.UUID
    ) -> None: ...

    async def ensure_conversation(
        self, user_id: uuid.UUID, client_id: str, external_id: str | None
    ) -> uuid.UUID: ...

    async def start_call(self, call: CallStart) -> None: ...

    async def finish_call(self, call: CallStart, result: CallResult) -> None: ...


def _audit(
    *,
    action: str,
    outcome: str,
    request_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_client: str | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid7(),
        "action": action,
        "outcome": outcome,
        "request_id": request_id,
        "actor_user_id": actor_user_id,
        "actor_client": actor_client,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details or {},
    }


class PgStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("select 1"))

    async def resolve_user(self, principal: Principal, request_id: uuid.UUID) -> User:
        """Пользователь по (iss, sub); перенос по (entra_tid, entra_oid); иначе создание.

        Гонка двух первых запросов одного пользователя решается уникальностью (iss, sub):
        проигравшая транзакция откатывается и повторяет поиск.
        """
        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    return await self._resolve(conn, principal, request_id)
            except IntegrityError:
                if attempt == 2:
                    raise
                log.info("гонка при создании пользователя, повтор")
        raise AssertionError("недостижимо")

    async def _resolve(
        self, conn: AsyncConnection, principal: Principal, request_id: uuid.UUID
    ) -> User:
        row = (
            await conn.execute(
                select(
                    users.c.id,
                    users.c.status,
                    users.c.display_name,
                    users.c.email,
                    users.c.last_seen_at,
                    identities.c.id.label("identity_id"),
                    identities.c.entra_tid,
                    identities.c.entra_oid,
                    identities.c.idp,
                )
                .join(identities, identities.c.user_id == users.c.id)
                .where(
                    identities.c.issuer == principal.issuer,
                    identities.c.subject == principal.subject,
                )
            )
        ).one_or_none()
        if row is not None:
            await self._touch(conn, row, principal)
            return User(id=row.id, status=row.status)

        if principal.entra_tid and principal.entra_oid:
            moved = (
                await conn.execute(
                    select(
                        identities.c.id, identities.c.user_id, identities.c.subject, users.c.status
                    )
                    .join(users, users.c.id == identities.c.user_id)
                    .where(
                        identities.c.entra_tid == principal.entra_tid,
                        identities.c.entra_oid == principal.entra_oid,
                    )
                    .with_for_update(of=identities)
                )
            ).one_or_none()
            if moved is not None:
                await conn.execute(
                    update(identities)
                    .where(identities.c.id == moved.id)
                    .values(
                        issuer=principal.issuer,
                        subject=principal.subject,
                        idp=principal.idp,
                        last_seen_at=func.now(),
                    )
                )
                await conn.execute(
                    insert(audit_events).values(
                        _audit(
                            action="identity.rebound",
                            outcome="allow",
                            request_id=request_id,
                            actor_user_id=moved.user_id,
                            actor_client=principal.client_id,
                            resource_type="identity",
                            resource_id=str(moved.id),
                            details={"previous_subject": moved.subject, "idp": principal.idp},
                        )
                    )
                )
                log.info("идентичность перенесена на новый sub user_id=%s", moved.user_id)
                return User(id=moved.user_id, status=moved.status)

        user_id = uuid.uuid7()
        await conn.execute(
            insert(users).values(
                id=user_id, display_name=principal.name, email=principal.email, status="active"
            )
        )
        identity_id = uuid.uuid7()
        await conn.execute(
            insert(identities).values(
                id=identity_id,
                user_id=user_id,
                issuer=principal.issuer,
                subject=principal.subject,
                idp=principal.idp,
                entra_tid=principal.entra_tid,
                entra_oid=principal.entra_oid,
            )
        )
        await conn.execute(
            insert(audit_events).values(
                _audit(
                    action="user.created",
                    outcome="allow",
                    request_id=request_id,
                    actor_user_id=user_id,
                    actor_client=principal.client_id,
                    resource_type="user",
                    resource_id=str(user_id),
                    details={"idp": principal.idp, "identity_id": str(identity_id)},
                )
            )
        )
        log.info("создан пользователь user_id=%s idp=%s", user_id, principal.idp)
        return User(id=user_id, status="active")

    async def _touch(self, conn: AsyncConnection, row: Any, principal: Principal) -> None:  # noqa: ANN401
        changes: dict[str, Any] = {}
        if principal.name and principal.name != row.display_name:
            changes["display_name"] = principal.name
        if principal.email and principal.email != row.email:
            changes["email"] = principal.email
        now = dt.datetime.now(dt.UTC)
        if changes or now - row.last_seen_at > _SEEN_INTERVAL:
            await conn.execute(
                update(users).where(users.c.id == row.id).values(last_seen_at=func.now(), **changes)
            )
            ident: dict[str, Any] = {"last_seen_at": func.now()}
            # Атрибуты Entra появляются после первого входа через брокер; не стираем их. Если та же
            # пара уже у другой идентичности, не дописываем: иначе нарушение уникальности на
            # каждом запросе (500 навсегда). Разбирает администратор по журналу.
            if principal.entra_tid and principal.entra_oid and not row.entra_oid:
                taken = (
                    await conn.execute(
                        select(identities.c.id).where(
                            identities.c.entra_tid == principal.entra_tid,
                            identities.c.entra_oid == principal.entra_oid,
                        )
                    )
                ).scalar_one_or_none()
                if taken is None:
                    ident |= {"entra_tid": principal.entra_tid, "entra_oid": principal.entra_oid}
                else:
                    log.warning(
                        "entra_oid уже у другой идентичности %s, user_id=%s не изменён",
                        taken,
                        row.id,
                    )
            if principal.idp and principal.idp != row.idp:
                ident["idp"] = principal.idp
            await conn.execute(
                update(identities).where(identities.c.id == row.identity_id).values(**ident)
            )

    async def record_denied(self, principal: Principal, reason: str, request_id: uuid.UUID) -> None:
        """Отказ по валидному токену: пользователь известен IdP, но доступа нет."""
        async with self._engine.begin() as conn:
            user_id = (
                await conn.execute(
                    select(identities.c.user_id).where(
                        identities.c.issuer == principal.issuer,
                        identities.c.subject == principal.subject,
                    )
                )
            ).scalar_one_or_none()
            await conn.execute(
                insert(audit_events).values(
                    _audit(
                        action="auth.token",
                        outcome="deny",
                        request_id=request_id,
                        actor_user_id=user_id,
                        actor_client=principal.client_id or None,
                        details={"reason": reason, "subject": principal.subject},
                    )
                )
            )

    async def ensure_conversation(
        self, user_id: uuid.UUID, client_id: str, external_id: str | None
    ) -> uuid.UUID:
        async with self._engine.begin() as conn:
            if external_id is None:
                conv_id = uuid.uuid7()
                await conn.execute(
                    insert(conversations).values(id=conv_id, user_id=user_id, client_id=client_id)
                )
                return conv_id
            stmt = (
                pg_insert(conversations)
                .values(
                    id=uuid.uuid7(), user_id=user_id, client_id=client_id, external_id=external_id
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "client_id", "external_id"],
                    set_={"updated_at": func.now()},
                )
                .returning(conversations.c.id)
            )
            conv: uuid.UUID = (await conn.execute(stmt)).scalar_one()
            return conv

    async def start_call(self, call: CallStart) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(model_calls).values(
                    id=call.id,
                    user_id=call.user_id,
                    conversation_id=call.conversation_id,
                    client_id=call.client_id,
                    model=call.model,
                    stream=call.stream,
                    status="started",
                )
            )

    async def finish_call(self, call: CallStart, result: CallResult) -> None:
        """Итог вызова, новые сообщения и запись аудита в одной транзакции."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(model_calls)
                .where(model_calls.c.id == call.id)
                .values(
                    status=result.status,
                    http_status=result.http_status,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cost_usd=result.cost_usd,
                    gateway_request_id=result.gateway_request_id,
                    error=result.error,
                    finished_at=func.now(),
                )
            )
            history = list(call.new_messages)
            if result.response_message is not None:
                history.append(result.response_message)
            if history and result.status in ("ok", "cancelled"):
                await conn.execute(
                    insert(messages),
                    [
                        {
                            "id": uuid.uuid7(),
                            "conversation_id": call.conversation_id,
                            "model_call_id": call.id,
                            "position": pos,
                            "role": msg["role"],
                            "content": msg,
                        }
                        for pos, msg in enumerate(history)
                    ],
                )
            await conn.execute(
                update(conversations)
                .where(conversations.c.id == call.conversation_id)
                .values(updated_at=func.now())
            )
            await conn.execute(
                insert(audit_events).values(
                    _audit(
                        action="model.call",
                        outcome="allow" if result.status in ("ok", "cancelled") else "error",
                        request_id=call.id,
                        actor_user_id=call.user_id,
                        actor_client=call.client_id,
                        resource_type="model",
                        resource_id=call.model,
                        details={
                            "status": result.status,
                            "http_status": result.http_status,
                            "stream": call.stream,
                            "conversation_id": str(call.conversation_id),
                            "prompt_tokens": result.prompt_tokens,
                            "completion_tokens": result.completion_tokens,
                            "cost_usd": str(result.cost_usd)
                            if result.cost_usd is not None
                            else None,
                        },
                    )
                )
            )
