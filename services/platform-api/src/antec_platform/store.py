"""Хранилище Platform API: пользователи, история, вызовы моделей, аудит (ADR-0006)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, insert, null, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from antec_platform.agents import PendingCall, RunOutcome
from antec_platform.auth import Principal
from antec_platform.db import (
    agent_runs,
    audit_events,
    conversations,
    identities,
    messages,
    model_calls,
    users,
)

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
    # Класс данных запроса (ADR-0011): в аудит model.call.
    data_class: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRunStart:
    """Запуск агента (ADR-0010, п. 5): id = id запроса."""

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    client_id: str
    agent: str
    model: str
    stream: bool
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    # Продолжение запуска после подтверждения (ADR-0010, п. 7).
    parent_run_id: uuid.UUID | None = None
    data_class: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """Код и срок подтверждения запуска в статусе awaiting_approval."""

    code: str
    expires_at: dt.datetime


@dataclass(frozen=True, slots=True)
class PendingRun:
    """Запуск, ждущий решения человека."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    agent: str
    code: str
    expires_at: dt.datetime
    state: dict[str, Any]
    pending: list[PendingCall]


class Store(Protocol):
    async def ping(self) -> None: ...

    async def resolve_user(self, principal: Principal, request_id: uuid.UUID) -> User: ...

    async def record_denied(
        self, principal: Principal, reason: str, request_id: uuid.UUID
    ) -> None: ...

    async def record_policy_deny(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: str,
        request_id: uuid.UUID,
        scope: str | None = None,
        reason: str = "policy",
        data_class: str | None = None,
    ) -> None: ...

    async def ensure_conversation(
        self, user_id: uuid.UUID, client_id: str, external_id: str | None
    ) -> uuid.UUID: ...

    async def start_call(self, call: CallStart) -> None: ...

    async def finish_call(self, call: CallStart, result: CallResult) -> None: ...

    async def start_agent_run(self, run: AgentRunStart) -> None: ...

    async def finish_agent_run(
        self, run: AgentRunStart, outcome: RunOutcome, approval: Approval | None = None
    ) -> None: ...

    async def find_pending_run(
        self,
        user_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> PendingRun | None: ...

    async def resolve_pending_run(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
        *,
        client_id: str,
        request_id: uuid.UUID,
    ) -> PendingRun | None: ...

    async def record_tool_call(
        self,
        *,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        client_id: str,
        run_id: uuid.UUID,
        agent: str,
        ref: str,
        outcome: str,
        details: dict[str, Any],
        call_id: uuid.UUID | None = None,
        started: bool = False,
    ) -> None: ...

    async def expire_pending_runs(
        self, user_id: uuid.UUID, *, client_id: str, request_id: uuid.UUID
    ) -> int: ...

    async def conversation_class(self, conversation_id: uuid.UUID) -> str | None: ...

    async def raise_conversation_class(
        self, conversation_id: uuid.UUID, data_class: str
    ) -> None: ...


def audit_row(
    *,
    action: str,
    outcome: str,
    request_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    actor_client: str | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    call_id: uuid.UUID | None = None,
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
        "call_id": call_id,
    }


def _closed(pending: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Ожидающие вызовы после решения: без аргументов (ADR-0012, п. 1)."""
    return [{"call_id": c.get("call_id"), "ref": c.get("ref")} for c in pending or []]


async def expire_pending(
    conn: AsyncConnection,
    *,
    request_id: uuid.UUID,
    actor_client: str,
    user_id: uuid.UUID | None = None,
    limit: int = 500,
) -> int:
    """Истёкшие ожидания = expired, состояние и аргументы удаляются, событие tool.approval.

    Вызывается запросом пользователя (его ожидания) и platform-worker (все). Блокировка строк
    с skip locked: решение, принимаемое в этот момент, не перебивается.
    """
    stmt = select(
        agent_runs.c.id, agent_runs.c.user_id, agent_runs.c.agent, agent_runs.c.pending
    ).where(
        agent_runs.c.status == "awaiting_approval",
        agent_runs.c.approval_expires_at <= func.now(),
    )
    if user_id is not None:
        stmt = stmt.where(agent_runs.c.user_id == user_id)
    rows = (await conn.execute(stmt.limit(limit).with_for_update(skip_locked=True))).all()
    for row in rows:
        await conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == row.id)
            .values(status="expired", state=null(), pending=_closed(row.pending))
        )
    if rows:
        await conn.execute(
            insert(audit_events),
            [
                audit_row(
                    action="tool.approval",
                    outcome="deny",
                    request_id=request_id,
                    # Решил срок, а не пользователь; чей запуск, видно по agent_run и details.
                    actor_user_id=user_id,
                    actor_client=actor_client,
                    resource_type="agent_run",
                    resource_id=str(row.id),
                    details={
                        "decision": "expired",
                        "agent": row.agent,
                        "user_id": str(row.user_id),
                        "tools": [c["ref"] for c in row.pending or []],
                    },
                )
                for row in rows
            ],
        )
    return len(rows)


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
                    select(identities.c.id, identities.c.user_id, users.c.status)
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
                        audit_row(
                            action="identity.rebound",
                            outcome="allow",
                            request_id=request_id,
                            actor_user_id=moved.user_id,
                            actor_client=principal.client_id,
                            resource_type="identity",
                            resource_id=str(moved.id),
                            details={"idp": principal.idp},
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
                audit_row(
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
                    audit_row(
                        action="auth.token",
                        outcome="deny",
                        request_id=request_id,
                        actor_user_id=user_id,
                        actor_client=principal.client_id or None,
                        # Идентификатор IdP только там, где нашего id ещё нет (ADR-0012, п. 5).
                        details={"reason": reason}
                        | ({"subject": principal.subject} if user_id is None else {}),
                    )
                )
            )

    async def record_policy_deny(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: str,
        request_id: uuid.UUID,
        scope: str | None = None,
        reason: str = "policy",
        data_class: str | None = None,
    ) -> None:
        """Отказ политики (ADR-0009, п. 6): кто, что и с какими ролями пытался сделать.

        `scope`: scope политик инсталляции (ADR-0008), чтобы отказ можно было отнести к
        политикам ядра или инсталляции той версии, что действовала.
        """
        details: dict[str, object] = {"action": action, "roles": list(principal.roles)}
        if scope:
            details["policy_scope"] = scope
        # Причина: policy (Cerbos) или data_class (инвариант ADR-0011).
        if reason != "policy":
            details["reason"] = reason
        if data_class:
            details["data_class"] = data_class
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(audit_events).values(
                    audit_row(
                        action="policy.deny",
                        outcome="deny",
                        request_id=request_id,
                        actor_user_id=user_id,
                        actor_client=principal.client_id or None,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        details=details,
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
                    audit_row(
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
                            "data_class": call.data_class,
                            "prompt_tokens": result.prompt_tokens,
                            "completion_tokens": result.completion_tokens,
                            "cost_usd": str(result.cost_usd)
                            if result.cost_usd is not None
                            else None,
                        },
                    )
                )
            )

    async def start_agent_run(self, run: AgentRunStart) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(agent_runs).values(
                    id=run.id,
                    user_id=run.user_id,
                    conversation_id=run.conversation_id,
                    client_id=run.client_id,
                    agent=run.agent,
                    model=run.model,
                    stream=run.stream,
                    status="started",
                    parent_run_id=run.parent_run_id,
                )
            )

    async def finish_agent_run(
        self, run: AgentRunStart, outcome: RunOutcome, approval: Approval | None = None
    ) -> None:
        """Итог запуска, шаги в model_calls, история и аудит в одной транзакции (ADR-0010, п. 5).

        Каждый шаг = строка model_calls со ссылкой agent_run_id и событие model.call; запуск
        целиком = событие agent.run. История (новые сообщения пользователя и ответ агента)
        привязана к последнему шагу; без шагов истории нет.
        """
        ok = outcome.status in ("ok", "cancelled", "awaiting_approval")
        waiting: dict[str, Any] = {}
        if outcome.status == "awaiting_approval" and approval is not None:
            waiting = {
                "state": outcome.state,
                "pending": [
                    {"call_id": c.call_id, "ref": c.ref, "arguments": c.arguments}
                    for c in outcome.pending
                ],
                "approval_code": approval.code,
                "approval_expires_at": approval.expires_at,
            }
        async with self._engine.begin() as conn:
            await conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .values(
                    status=outcome.status,
                    steps=len(outcome.steps),
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    # Текст ошибки может содержать ввод: хранится только код (ADR-0012).
                    error=outcome.error_code,
                    finished_at=func.now(),
                    **waiting,
                )
            )
            step_ids: list[uuid.UUID] = []
            # Аудит последним в транзакции (ADR-0012, п. 4): строки разговоров уже обновлены.
            events: list[dict[str, Any]] = []
            for number, step in enumerate(outcome.steps, start=1):
                step_id = uuid.uuid7()
                step_ids.append(step_id)
                await conn.execute(
                    insert(model_calls).values(
                        id=step_id,
                        user_id=run.user_id,
                        conversation_id=run.conversation_id,
                        client_id=run.client_id,
                        model=run.model,
                        stream=run.stream,
                        status=step.status,
                        http_status=step.http_status,
                        prompt_tokens=step.prompt_tokens,
                        completion_tokens=step.completion_tokens,
                        gateway_request_id=step.gateway_request_id,
                        error=step.error,
                        agent_run_id=run.id,
                        started_at=step.started_at or func.now(),
                        finished_at=step.finished_at or func.now(),
                    )
                )
                events.append(
                    audit_row(
                        action="model.call",
                        outcome="allow" if step.status in ("ok", "cancelled") else "error",
                        request_id=run.id,
                        actor_user_id=run.user_id,
                        actor_client=run.client_id,
                        resource_type="model",
                        resource_id=run.model,
                        details={
                            "status": step.status,
                            "http_status": step.http_status,
                            "stream": run.stream,
                            "conversation_id": str(run.conversation_id),
                            "agent": run.agent,
                            "agent_run_id": str(run.id),
                            "data_class": run.data_class,
                            "step": number,
                            "model_call_id": str(step_id),
                            "prompt_tokens": step.prompt_tokens,
                            "completion_tokens": step.completion_tokens,
                        },
                    )
                )
            history = list(run.new_messages)
            if outcome.text:
                history.append({"role": "assistant", "content": outcome.text})
            if ok and step_ids and history:
                await conn.execute(
                    insert(messages),
                    [
                        {
                            "id": uuid.uuid7(),
                            "conversation_id": run.conversation_id,
                            "model_call_id": step_ids[-1],
                            "position": pos,
                            "role": msg["role"],
                            "content": msg,
                        }
                        for pos, msg in enumerate(history)
                    ],
                )
            await conn.execute(
                update(conversations)
                .where(conversations.c.id == run.conversation_id)
                .values(updated_at=func.now())
            )
            events.append(
                audit_row(
                    action="agent.run",
                    outcome="allow" if ok else "error",
                    request_id=run.id,
                    actor_user_id=run.user_id,
                    actor_client=run.client_id,
                    resource_type="agent",
                    resource_id=run.agent,
                    details={
                        "status": outcome.status,
                        "model": run.model,
                        "stream": run.stream,
                        "conversation_id": str(run.conversation_id),
                        "data_class": run.data_class,
                        "steps": len(outcome.steps),
                        "prompt_tokens": outcome.prompt_tokens,
                        "completion_tokens": outcome.completion_tokens,
                        "error_code": outcome.error_code,
                        "pending_tools": [c.ref for c in outcome.pending],
                        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
                    },
                )
            )
            await conn.execute(insert(audit_events), events)

    async def find_pending_run(
        self,
        user_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> PendingRun | None:
        """Последний запуск пользователя в статусе awaiting_approval (в разговоре или по id)."""
        stmt = select(agent_runs).where(
            agent_runs.c.user_id == user_id, agent_runs.c.status == "awaiting_approval"
        )
        if conversation_id is not None:
            stmt = stmt.where(agent_runs.c.conversation_id == conversation_id)
        if run_id is not None:
            stmt = stmt.where(agent_runs.c.id == run_id)
        stmt = stmt.order_by(agent_runs.c.started_at.desc()).limit(1)
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return _pending(row) if row is not None else None

    async def resolve_pending_run(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
        *,
        client_id: str,
        request_id: uuid.UUID,
    ) -> PendingRun | None:
        """Решение по ожиданию ровно один раз: блокировка строки только в awaiting_approval.

        Два одновременных подтверждения: выигрывает одно, второе получает None. Решение
        записывается событием tool.approval (кто, что, по каким инструментам).
        """
        if status not in ("approved", "denied", "expired"):
            raise ValueError(status)
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(agent_runs)
                    .where(
                        agent_runs.c.id == run_id,
                        agent_runs.c.user_id == user_id,
                        agent_runs.c.status == "awaiting_approval",
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return None
            # Состояние и аргументы больше не нужны: продолжение получает их из этой строки.
            await conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(status=status, state=null(), pending=_closed(row.pending))
            )
            pending = _pending(row)
            await conn.execute(
                insert(audit_events).values(
                    audit_row(
                        action="tool.approval",
                        outcome="allow" if status == "approved" else "deny",
                        request_id=request_id,
                        actor_user_id=user_id,
                        actor_client=client_id,
                        resource_type="agent_run",
                        resource_id=str(run_id),
                        details={
                            "decision": status,
                            "agent": pending.agent,
                            "tools": [c.ref for c in pending.pending],
                        },
                    )
                )
            )
        return pending

    async def record_tool_call(
        self,
        *,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        client_id: str,
        run_id: uuid.UUID,
        agent: str,
        ref: str,
        outcome: str,
        details: dict[str, Any],
        call_id: uuid.UUID | None = None,
        started: bool = False,
    ) -> None:
        """Начало (`started`) или итог вызова инструмента (ADR-0012, п. 3).

        Без аргументов, результата и текстов ошибок (ADR-0012, п. 5).
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(audit_events).values(
                    audit_row(
                        action="tool.call.started" if started else "tool.call",
                        outcome=outcome,
                        request_id=request_id,
                        actor_user_id=user_id,
                        actor_client=client_id,
                        resource_type="tool",
                        resource_id=ref,
                        details={"agent": agent, "agent_run_id": str(run_id), **details},
                        call_id=call_id,
                    )
                )
            )

    async def expire_pending_runs(
        self, user_id: uuid.UUID, *, client_id: str, request_id: uuid.UUID
    ) -> int:
        """Истёкшие ожидания пользователя = expired (ADR-0010, п. 7), во всех разговорах.

        Вызывается при каждом запросе пользователя к агенту; ожидания тех, кто не вернулся,
        закрывает platform-worker (ADR-0012, п. 2).
        """
        async with self._engine.begin() as conn:
            return await expire_pending(
                conn, request_id=request_id, actor_client=client_id, user_id=user_id
            )

    async def conversation_class(self, conversation_id: uuid.UUID) -> str | None:
        async with self._engine.connect() as conn:
            value = (
                await conn.execute(
                    select(conversations.c.data_class).where(conversations.c.id == conversation_id)
                )
            ).scalar_one_or_none()
        return str(value) if value is not None else None

    async def raise_conversation_class(self, conversation_id: uuid.UUID, data_class: str) -> None:
        """Класс разговора только растёт (ADR-0011): одно условное UPDATE, без гонок."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(_RAISE_CLASS_SQL),
                {"c": data_class, "id": conversation_id},
            )


# Порядок классов ADR-0011 (data_classes.CLASSES); значение класса только параметром.
_RAISE_CLASS_SQL = (
    "update conversations set data_class = :c where id = :id and (data_class is null or "
    "array_position(array['public', 'internal', 'confidential', 'restricted'], data_class) "
    "< array_position(array['public', 'internal', 'confidential', 'restricted'], "
    "cast(:c as text)))"
)


def _pending(row: Any) -> PendingRun:  # noqa: ANN401 - строка SQLAlchemy
    return PendingRun(
        id=row.id,
        conversation_id=row.conversation_id,
        agent=row.agent,
        code=row.approval_code or "",
        expires_at=row.approval_expires_at,
        state=row.state or {},
        pending=[
            PendingCall(call_id=c["call_id"], ref=c["ref"], arguments=c.get("arguments") or {})
            for c in row.pending or []
        ],
    )
