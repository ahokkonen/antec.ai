"""Схема и хранилище на настоящем PostgreSQL (маркер db).

Нужна переменная PLATFORM_TEST_DATABASE_URL с суперпользователем, например
postgresql+asyncpg://postgres:pw@127.0.0.1:55432/postgres. Тест создаёт свои роли и БД и
удаляет их после себя.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import Connection, make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from antec_platform.agents import RunOutcome, StepRecord
from antec_platform.auth import Principal
from antec_platform.db import (
    agent_runs,
    audit_events,
    create_engine,
    messages,
    metadata,
    model_calls,
)
from antec_platform.migrate import upgrade
from antec_platform.settings import DatabaseSettings
from antec_platform.store import AgentRunStart, CallResult, CallStart, PgStore

pytestmark = [pytest.mark.db, pytest.mark.anyio]

ADMIN_URL = os.environ.get("PLATFORM_TEST_DATABASE_URL")
if not ADMIN_URL:
    pytest.skip("PLATFORM_TEST_DATABASE_URL не задан", allow_module_level=True)

OWNER = "platform_test_owner"
APP = "platform_test_app"


def _settings(user: str, password: str, database: str) -> DatabaseSettings:
    url = make_url(ADMIN_URL or "")
    return DatabaseSettings(
        database_host=url.host or "127.0.0.1",
        database_port=url.port or 5432,
        database_name=database,
        database_user=user,
        database_password=SecretStr(password),
        app_role=APP,
    )


async def _admin(sql: list[str], database: str | None = None) -> None:
    url = make_url(ADMIN_URL or "")
    if database:
        url = url.set(database=database)
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for stmt in sql:
                await conn.execute(text(stmt))
    finally:
        await engine.dispose()


class Db:
    def __init__(self, owner: DatabaseSettings, app: DatabaseSettings) -> None:
        self.owner = owner
        self.app = app


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    name = f"platform_test_{secrets.token_hex(4)}"
    owner_pw, app_pw = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    await _admin(
        [
            f"drop role if exists {APP}",
            f"drop role if exists {OWNER}",
            f"create role {OWNER} login password '{owner_pw}'",
            f"create role {APP} login password '{app_pw}'",
            f"create database {name} owner {OWNER}",
            f"revoke all on database {name} from public",
            f"grant connect on database {name} to {OWNER}, {APP}",
        ]
    )
    await _admin(
        ["revoke all on schema public from public", f"alter schema public owner to {OWNER}"],
        database=name,
    )
    owner = _settings(OWNER, owner_pw, name)
    await asyncio.to_thread(upgrade, owner)
    try:
        yield Db(owner, _settings(APP, app_pw, name))
    finally:
        await _admin(
            [f"drop database {name} with (force)", f"drop role {APP}", f"drop role {OWNER}"]
        )


@pytest.fixture
async def app_engine(db: Db) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db.app)
    yield engine
    await engine.dispose()


@pytest.fixture
async def owner_engine(db: Db) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db.owner)
    yield engine
    await engine.dispose()


def _principal(sub: str, oid: str | None = None) -> Principal:
    return Principal(
        issuer="http://localhost:8080/realms/antec",
        subject=sub,
        client_id="open-webui",
        idp="entra" if oid else None,
        entra_tid="tid" if oid else None,
        entra_oid=oid,
        name="Имя",
        email="a@example.test",
    )


async def test_schema_matches_metadata(owner_engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, metadata))

    async with owner_engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_migrations_are_idempotent(db: Db) -> None:
    await asyncio.to_thread(upgrade, db.owner)


@pytest.mark.parametrize(
    "stmt",
    [
        "delete from users",
        "delete from agent_runs",
        "update audit_events set action = 'x'",
        "delete from audit_events",
        "create table x (id int)",
        "select * from alembic_version",
    ],
)
async def test_app_role_privileges(app_engine: AsyncEngine, stmt: str) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(stmt))


@pytest.mark.parametrize(
    "stmt",
    [
        "update audit_events set action = 'x'",
        "delete from audit_events",
        "truncate audit_events",
    ],
)
async def test_audit_append_only_for_owner(owner_engine: AsyncEngine, stmt: str) -> None:
    store = PgStore(owner_engine)
    await store.resolve_user(_principal(f"owner-{uuid.uuid4()}"), uuid.uuid7())
    async with owner_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="только добавление"):
            await conn.execute(text(stmt))


async def test_resolve_user_create_repeat_rebind(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    oid = f"oid-{uuid.uuid4()}"
    first = await store.resolve_user(_principal("sub-a", oid), uuid.uuid7())
    again = await store.resolve_user(_principal("sub-a", oid), uuid.uuid7())
    assert again == first
    # Realm пересоздан: новый sub, тот же пользователь Entra.
    moved = await store.resolve_user(_principal("sub-b", oid), uuid.uuid7())
    assert moved.id == first.id
    async with app_engine.connect() as conn:
        actions = (
            (
                await conn.execute(
                    select(audit_events.c.action)
                    .where(audit_events.c.actor_user_id == first.id)
                    .order_by(audit_events.c.id)
                )
            )
            .scalars()
            .all()
        )
    assert actions == ["user.created", "identity.rebound"]
    # Локальный пользователь без Entra после смены sub становится новым.
    local = await store.resolve_user(_principal("local-1"), uuid.uuid7())
    assert (await store.resolve_user(_principal("local-2"), uuid.uuid7())).id != local.id


async def test_concurrent_first_requests_create_one_user(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    principal = _principal(f"race-{uuid.uuid4()}", f"oid-{uuid.uuid4()}")
    results = await asyncio.gather(*(store.resolve_user(principal, uuid.uuid7()) for _ in range(5)))
    assert len({r.id for r in results}) == 1


async def test_call_history_and_audit(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    user = await store.resolve_user(_principal(f"hist-{uuid.uuid4()}"), uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", "chat-1")
    assert await store.ensure_conversation(user.id, "open-webui", "chat-1") == conv
    assert await store.ensure_conversation(user.id, "open-webui", None) != conv
    call = CallStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        model="chat-fast",
        stream=True,
        new_messages=[{"role": "user", "content": "Привет"}],
    )
    await store.start_call(call)
    await store.finish_call(
        call,
        CallResult(
            status="ok",
            http_status=200,
            prompt_tokens=5,
            completion_tokens=2,
            response_message={"role": "assistant", "content": "Здравствуйте"},
        ),
    )
    failed = replace(call, id=uuid.uuid7())
    await store.start_call(failed)
    await store.finish_call(failed, CallResult(status="rejected", http_status=400, error="budget"))
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                select(messages.c.role, messages.c.content)
                .where(messages.c.conversation_id == conv)
                .order_by(messages.c.model_call_id, messages.c.position)
            )
        ).all()
        statuses = dict(
            (
                await conn.execute(
                    select(model_calls.c.id, model_calls.c.status).where(
                        model_calls.c.conversation_id == conv
                    )
                )
            ).all()
        )
        audit = (
            await conn.execute(
                select(audit_events.c.outcome, audit_events.c.details["status"].astext)
                .where(
                    audit_events.c.action == "model.call", audit_events.c.actor_user_id == user.id
                )
                .order_by(audit_events.c.id)
            )
        ).all()
    assert [(r.role, r.content["content"]) for r in rows] == [
        ("user", "Привет"),
        ("assistant", "Здравствуйте"),
    ]
    assert statuses == {call.id: "ok", failed.id: "rejected"}
    assert [tuple(a) for a in audit] == [("allow", "ok"), ("error", "rejected")]


async def test_entra_attrs_conflict_does_not_break_user(app_engine: AsyncEngine) -> None:
    """Пара Entra уже у другой идентичности: дописывание пропускается, а не падает каждый раз."""
    store = PgStore(app_engine)
    oid = f"oid-{uuid.uuid4()}"
    local = await store.resolve_user(_principal(f"local-{uuid.uuid4()}"), uuid.uuid7())
    await store.resolve_user(_principal(f"broker-{uuid.uuid4()}", oid), uuid.uuid7())
    # Тот же локальный пользователь получил атрибуты Entra, занятые другой идентичностью.
    async with app_engine.begin() as conn:
        await conn.execute(text("update users set last_seen_at = now() - interval '1 hour'"))
    sub = await _sub_of(app_engine, local.id)
    again = await store.resolve_user(_principal(sub, oid), uuid.uuid7())
    assert again.id == local.id


async def _sub_of(engine: AsyncEngine, user_id: uuid.UUID) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text("select subject from identities where user_id = :u"), {"u": user_id}
                )
            ).scalar_one()
        )


async def test_policy_deny_audited(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    principal = replace(_principal(f"deny-{uuid.uuid4()}"), roles=("open-webui-user",))
    user = await store.resolve_user(principal, uuid.uuid7())
    rid = uuid.uuid7()
    await store.record_policy_deny(principal, user.id, "invoke", "model", "chat-default", rid)
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    audit_events.c.action,
                    audit_events.c.outcome,
                    audit_events.c.resource_id,
                    audit_events.c.details,
                ).where(audit_events.c.request_id == rid)
            )
        ).one()
    assert (row.action, row.outcome, row.resource_id) == ("policy.deny", "deny", "chat-default")
    assert row.details == {"action": "invoke", "roles": ["open-webui-user"]}

    rid = uuid.uuid7()
    await store.record_policy_deny(
        principal, user.id, "invoke", "model", "chat-lab", rid, scope="dev"
    )
    async with app_engine.connect() as conn:
        details = (
            await conn.execute(
                select(audit_events.c.details).where(audit_events.c.request_id == rid)
            )
        ).scalar_one()
    assert details == {"action": "invoke", "roles": ["open-webui-user"], "policy_scope": "dev"}


async def test_agent_run_steps_history_and_audit(app_engine: AsyncEngine) -> None:
    """Запуск агента (ADR-0010, п. 5): agent_runs, шаги в model_calls, история, аудит."""
    store = PgStore(app_engine)
    principal = _principal(f"agent-{uuid.uuid4()}")
    user = await store.resolve_user(principal, uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", "chat-agent")
    run = AgentRunStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        agent="agent-assistant",
        model="chat-fast",
        stream=True,
        new_messages=[{"role": "user", "content": "Привет"}],
    )
    await store.start_agent_run(run)
    outcome = RunOutcome(
        status="ok",
        text="Здравствуйте",
        steps=[
            StepRecord(
                status="ok",
                http_status=200,
                prompt_tokens=10,
                completion_tokens=3,
                gateway_request_id="gw-1",
            ),
            StepRecord(
                status="ok",
                http_status=200,
                prompt_tokens=20,
                completion_tokens=4,
                gateway_request_id="gw-2",
            ),
        ],
    )
    await store.finish_agent_run(run, outcome)
    async with app_engine.connect() as conn:
        row = (await conn.execute(select(agent_runs).where(agent_runs.c.id == run.id))).one()
        calls = (
            await conn.execute(
                select(model_calls.c.id, model_calls.c.gateway_request_id, model_calls.c.status)
                .where(model_calls.c.agent_run_id == run.id)
                .order_by(model_calls.c.id)
            )
        ).all()
        history = (
            await conn.execute(
                select(messages.c.role, messages.c.model_call_id)
                .where(messages.c.conversation_id == conv)
                .order_by(messages.c.position)
            )
        ).all()
        audit = (
            await conn.execute(
                select(audit_events.c.action, audit_events.c.outcome)
                .where(audit_events.c.request_id == run.id)
                .order_by(audit_events.c.occurred_at, audit_events.c.id)
            )
        ).all()
    assert (row.status, row.steps, row.prompt_tokens, row.completion_tokens) == ("ok", 2, 30, 7)
    assert row.finished_at is not None
    assert [(c.gateway_request_id, c.status) for c in calls] == [("gw-1", "ok"), ("gw-2", "ok")]
    assert [h.role for h in history] == ["user", "assistant"]
    assert {h.model_call_id for h in history} == {calls[-1].id}
    assert [(a.action, a.outcome) for a in audit] == [
        ("model.call", "allow"),
        ("model.call", "allow"),
        ("agent.run", "allow"),
    ]


async def test_agent_run_failed_without_steps(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    principal = _principal(f"agent-fail-{uuid.uuid4()}")
    user = await store.resolve_user(principal, uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", None)
    run = AgentRunStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        agent="agent-assistant",
        model="chat-fast",
        stream=False,
        new_messages=[{"role": "user", "content": "q"}],
    )
    await store.start_agent_run(run)
    await store.finish_agent_run(
        run,
        RunOutcome(
            status="rejected",
            steps=[StepRecord(status="rejected", http_status=400)],
            error="Budget",
            http_status=400,
            error_code="upstream_error",
        ),
    )
    async with app_engine.connect() as conn:
        status = (
            await conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == run.id))
        ).scalar_one()
        n_messages = len(
            (
                await conn.execute(select(messages.c.id).where(messages.c.conversation_id == conv))
            ).all()
        )
        outcomes = (
            (
                await conn.execute(
                    select(audit_events.c.outcome).where(audit_events.c.request_id == run.id)
                )
            )
            .scalars()
            .all()
        )
    assert status == "rejected"
    assert n_messages == 0
    assert sorted(outcomes) == ["error", "error"]
