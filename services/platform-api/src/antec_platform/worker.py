"""Фоновый сервис platform-worker (ADR-0012, п. 2): то, что не завершил ни один запрос.

Каждый проход: истёкшие ожидания подтверждения, вызовы моделей и запуски агентов, оставшиеся в
started после сбоя, начатые вызовы инструментов без итога. Роль БД `platform_worker` не читает
тексты сообщений и состояние агентов; аудит только на добавление.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, null, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from antec_platform.db import agent_runs, audit_events, create_engine, model_calls
from antec_platform.retention import purge
from antec_platform.settings import WorkerSettings
from antec_platform.store import audit_row, expire_pending

log = logging.getLogger(__name__)

ACTOR = "platform-worker"
ABANDONED = "abandoned"
_BATCH = 500
# Очистка по срокам: раз в сутки, после сбоя повтор через час.
_PURGE_EVERY = dt.timedelta(days=1)
_PURGE_RETRY = dt.timedelta(hours=1)
# Обычный проход ищет незакрытые вызовы инструментов не глубже этого срока (таблица аудита
# растёт год); первый проход после запуска ищет за всё время.
_LOOKBACK = dt.timedelta(days=2)


@dataclass(frozen=True, slots=True)
class Sweep:
    expired: int
    model_calls: int
    agent_runs: int
    tool_calls: int


async def sweep(
    engine: AsyncEngine, abandon: dt.timedelta, lookback: dt.timedelta | None = None
) -> Sweep:
    """Один проход; каждая часть в своей транзакции."""
    pass_id = uuid.uuid7()
    async with engine.begin() as conn:
        expired = await expire_pending(conn, request_id=pass_id, actor_client=ACTOR, limit=_BATCH)
    async with engine.begin() as conn:
        calls = await _abandon_model_calls(conn, abandon)
    async with engine.begin() as conn:
        runs = await _abandon_agent_runs(conn, abandon)
    async with engine.begin() as conn:
        tools = await _unknown_tool_calls(conn, abandon, lookback)
    return Sweep(expired, calls, runs, tools)


def _lost(
    action: str,
    row: Any,  # noqa: ANN401 - строка SQLAlchemy
    resource_type: str,
    resource_id: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return audit_row(
        action=action,
        outcome="error",
        # request_id = id вызова или запуска: событие находится там же, где нашёлся бы итог.
        request_id=row.id,
        actor_user_id=row.user_id,
        actor_client=ACTOR,
        resource_type=resource_type,
        resource_id=resource_id,
        details={
            "status": "error",
            "reason": ABANDONED,
            "client_id": row.client_id,
            "conversation_id": str(row.conversation_id),
            **details,
        },
    )


async def _abandon_model_calls(conn: AsyncConnection, abandon: dt.timedelta) -> int:
    rows = (
        await conn.execute(
            select(
                model_calls.c.id,
                model_calls.c.user_id,
                model_calls.c.client_id,
                model_calls.c.conversation_id,
                model_calls.c.model,
                model_calls.c.agent_run_id,
            )
            .where(
                model_calls.c.status == "started", model_calls.c.started_at < func.now() - abandon
            )
            .limit(_BATCH)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if rows:
        await conn.execute(
            update(model_calls)
            .where(model_calls.c.id.in_([r.id for r in rows]))
            .values(status="error", error=ABANDONED, finished_at=func.now())
        )
        await conn.execute(
            insert(audit_events),
            [
                _lost(
                    "model.call",
                    r,
                    "model",
                    r.model,
                    {"agent_run_id": str(r.agent_run_id)} if r.agent_run_id else {},
                )
                for r in rows
            ],
        )
    return len(rows)


async def _abandon_agent_runs(conn: AsyncConnection, abandon: dt.timedelta) -> int:
    rows = (
        await conn.execute(
            select(
                agent_runs.c.id,
                agent_runs.c.user_id,
                agent_runs.c.client_id,
                agent_runs.c.conversation_id,
                agent_runs.c.agent,
                agent_runs.c.model,
            )
            .where(agent_runs.c.status == "started", agent_runs.c.started_at < func.now() - abandon)
            .limit(_BATCH)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if rows:
        await conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id.in_([r.id for r in rows]))
            .values(status="error", error=ABANDONED, state=null(), finished_at=func.now())
        )
        await conn.execute(
            insert(audit_events),
            [_lost("agent.run", r, "agent", r.agent, {"model": r.model}) for r in rows],
        )
    return len(rows)


async def _unknown_tool_calls(
    conn: AsyncConnection, abandon: dt.timedelta, lookback: dt.timedelta | None
) -> int:
    """Начатый вызов без итога: выполнено ли действие, неизвестно (tool.call.unknown).

    Отдельное действие, а не tool.call: поздний настоящий итог всё ещё можно записать.
    """
    done = audit_events.alias("done")
    since = true() if lookback is None else audit_events.c.occurred_at > func.now() - lookback
    rows = (
        await conn.execute(
            select(audit_events)
            .where(
                since,
                audit_events.c.action == "tool.call.started",
                audit_events.c.occurred_at < func.now() - abandon,
                ~select(done.c.id)
                .where(
                    done.c.call_id == audit_events.c.call_id,
                    done.c.action.in_(["tool.call", "tool.call.unknown"]),
                )
                .exists(),
            )
            .order_by(audit_events.c.occurred_at)
            .limit(_BATCH)
        )
    ).all()
    if rows:
        await conn.execute(
            pg_insert(audit_events).on_conflict_do_nothing(),
            [
                audit_row(
                    action="tool.call.unknown",
                    outcome="error",
                    request_id=r.request_id,
                    actor_user_id=r.actor_user_id,
                    actor_client=ACTOR,
                    resource_type=r.resource_type,
                    resource_id=r.resource_id,
                    details={**r.details, "reason": ABANDONED, "client_id": r.actor_client},
                    call_id=r.call_id,
                )
                for r in rows
            ],
        )
    return len(rows)


async def _purge_due(
    settings: WorkerSettings, engine: AsyncEngine, open_webui_engine: AsyncEngine | None
) -> dt.timedelta:
    """Очистка по срокам; возвращает, через сколько следующая. Сбой не мешает проходам."""
    try:
        counts = await purge(
            engine,
            content_days=settings.retention_content_days,
            metadata_days=settings.retention_metadata_days,
            audit_days=settings.retention_audit_days,
            open_webui_engine=open_webui_engine,
        )
        log.info("очистка по срокам: %s", counts)
        return _PURGE_EVERY
    except Exception:
        log.exception("очистка по срокам не выполнена, повтор через %s", _PURGE_RETRY)
        return _PURGE_RETRY


async def _purge_loop(
    settings: WorkerSettings, engine: AsyncEngine, open_webui_engine: AsyncEngine | None
) -> None:
    while True:
        delay = await _purge_due(settings, engine, open_webui_engine)
        await asyncio.sleep(delay.total_seconds())


async def run(
    settings: WorkerSettings,
    engine: AsyncEngine,
    stop: asyncio.Event,
    open_webui_engine: AsyncEngine | None = None,
) -> None:
    # Отдельной задачей: долгая очистка не задерживает проходы и отметку здоровья.
    purging = asyncio.create_task(_purge_loop(settings, engine, open_webui_engine))
    abandon = dt.timedelta(seconds=settings.worker_abandon_seconds)
    # Полный поиск, пока проход не исчерпал хвост (пачка меньше предела).
    full = True
    while not stop.is_set():
        try:
            lookback = None if full else abandon + _LOOKBACK
            result = await sweep(engine, abandon, lookback)
            full = full and result.tool_calls == _BATCH
            settings.worker_heartbeat_file.touch()
            if any((result.expired, result.model_calls, result.agent_runs, result.tool_calls)):
                log.warning("закрыто незавершённое: %s", result)
        except Exception:
            # Отметка не обновляется: healthcheck покажет, что проходы не выполняются.
            log.exception("проход platform-worker не выполнен")
            # За время сбоя незакрытое могло уйти глубже обычного поиска.
            full = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.worker_interval_seconds)
    purging.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await purging


async def serve(settings: WorkerSettings) -> None:
    engine = create_engine(settings, application_name=ACTOR)
    open_webui_engine = (
        create_engine(settings, ACTOR, settings.open_webui_database)
        if settings.open_webui_database
        else None
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        log.info("platform-worker запущен, интервал %s с", settings.worker_interval_seconds)
        await run(settings, engine, stop, open_webui_engine)
    finally:
        if open_webui_engine is not None:
            await open_webui_engine.dispose()
        await engine.dispose()
