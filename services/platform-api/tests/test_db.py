"""Схема и хранилище на настоящем PostgreSQL (маркер db).

Нужна переменная PLATFORM_TEST_DATABASE_URL с суперпользователем, например
postgresql+asyncpg://postgres:pw@127.0.0.1:55432/postgres. Тест создаёт свои роли и БД и
удаляет их после себя.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import Connection, make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from antec_platform import subject as subject_data
from antec_platform.agents import PendingCall, RunOutcome, StepRecord
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
from antec_platform.retention import purge
from antec_platform.settings import DatabaseSettings
from antec_platform.store import AgentRunStart, Approval, CallResult, CallStart, PgStore
from antec_platform.worker import sweep

pytestmark = [pytest.mark.db, pytest.mark.anyio]

ADMIN_URL = os.environ.get("PLATFORM_TEST_DATABASE_URL")
if not ADMIN_URL:
    pytest.skip("PLATFORM_TEST_DATABASE_URL не задан", allow_module_level=True)

OWNER = "platform_test_owner"
APP = "platform_test_app"
WORKER = "platform_test_worker"
SUBJECT = "platform_test_subject"


def _settings(user: str, password: str, database: str) -> DatabaseSettings:
    url = make_url(ADMIN_URL or "")
    return DatabaseSettings(
        database_host=url.host or "127.0.0.1",
        database_port=url.port or 5432,
        database_name=database,
        database_user=user,
        database_password=SecretStr(password),
        app_role=APP,
        worker_role=WORKER,
        subject_role=SUBJECT,
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
    def __init__(
        self,
        owner: DatabaseSettings,
        app: DatabaseSettings,
        worker: DatabaseSettings,
        subject: DatabaseSettings,
    ) -> None:
        self.owner = owner
        self.app = app
        self.worker = worker
        self.subject = subject


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Db]:
    name = f"platform_test_{secrets.token_hex(4)}"
    owner_pw, app_pw, worker_pw, subject_pw = (secrets.token_urlsafe(16) for _ in range(4))
    await _admin(
        [
            f"drop role if exists {APP}",
            f"drop role if exists {WORKER}",
            f"drop role if exists {SUBJECT}",
            f"drop role if exists {OWNER}",
            f"create role {OWNER} login password '{owner_pw}'",
            f"create role {APP} login password '{app_pw}'",
            f"create role {WORKER} login password '{worker_pw}'",
            f"create role {SUBJECT} login password '{subject_pw}'",
            f"create database {name} owner {OWNER}",
            f"revoke all on database {name} from public",
            f"grant connect on database {name} to {OWNER}, {APP}, {WORKER}, {SUBJECT}",
        ]
    )
    await _admin(
        ["revoke all on schema public from public", f"alter schema public owner to {OWNER}"],
        database=name,
    )
    owner = _settings(OWNER, owner_pw, name)
    await asyncio.to_thread(upgrade, owner)
    try:
        yield Db(
            owner,
            _settings(APP, app_pw, name),
            _settings(WORKER, worker_pw, name),
            _settings(SUBJECT, subject_pw, name),
        )
    finally:
        await _admin(
            [f"drop database {name} with (force)", f"drop role {APP}, {WORKER}, {SUBJECT}, {OWNER}"]
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


@pytest.fixture
async def worker_engine(db: Db) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db.worker)
    yield engine
    await engine.dispose()


@pytest.fixture
async def subject_engine(db: Db) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db.subject)
    yield engine
    await engine.dispose()


# Таблицы Open WebUI 0.13.3, к которым обращается adapters/open_webui.py (столбцы как в его БД).
_OPEN_WEBUI_DDL = [
    'create table "user" (id text primary key, oauth json)',
    "create table chat (id varchar primary key, user_id varchar, title text, created_at bigint, "
    "updated_at bigint, chat json, summary text)",
    "create table chat_message (id text primary key, chat_id text not null "
    "references chat(id) on delete cascade, content json)",
    "create table shared_chat (id text primary key, chat_id text not null "
    "references chat(id) on delete cascade)",
    "create table automation_run (id text primary key, chat_id text)",
    "create table comment (id text primary key, resource_type text, resource_id text)",
    "create table comment_reaction (id text primary key, comment_id text)",
    "create table notification (id text primary key, resource_type text, resource_id text)",
    "create table chat_view_state (user_id text, chat_id text)",
    "create table chatidtag (id varchar primary key, chat_id varchar)",
]
_GRANTS = Path(__file__).parents[3] / "deploy/compose/open-webui/retention-grants.sql"


@pytest.fixture
async def open_webui(db: Db) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine, AsyncEngine]]:
    """БД как у Open WebUI с настоящим файлом прав: движки админа, worker, subject."""
    name = f"openwebui_test_{secrets.token_hex(4)}"
    await _admin([f"create database {name}"])
    sql = (
        _GRANTS.read_text().replace("platform_worker", WORKER).replace("platform_subject", SUBJECT)
    )
    grants = [
        s.strip()
        for s in "\n".join(
            line for line in sql.splitlines() if not line.startswith(("--", "\\"))
        ).split(";")
        if s.strip() and s.strip() not in ("BEGIN", "COMMIT")
    ]
    await _admin([*_OPEN_WEBUI_DDL, *grants], database=name)
    admin = create_async_engine(make_url(ADMIN_URL or "").set(database=name), poolclass=NullPool)
    worker = create_engine(db.worker, database=name)
    subject = create_engine(db.subject, database=name)
    try:
        yield admin, worker, subject
    finally:
        for engine in (admin, worker, subject):
            await engine.dispose()
        await _admin([f"drop database {name} with (force)"])


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
                started_at=dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC),
                finished_at=dt.datetime(2026, 11, 22, 12, 0, 3, tzinfo=dt.UTC),
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
                select(
                    model_calls.c.id,
                    model_calls.c.gateway_request_id,
                    model_calls.c.status,
                    model_calls.c.started_at,
                    model_calls.c.finished_at,
                )
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
    assert calls[0].started_at == dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)
    assert (calls[0].finished_at - calls[0].started_at).total_seconds() == 3
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


async def test_pending_run_lifecycle_and_tool_audit(app_engine: AsyncEngine) -> None:
    """Ожидание подтверждения (ADR-0010, п. 7): запись, поиск, решение ровно один раз."""
    store = PgStore(app_engine)
    principal = _principal(f"approve-{uuid.uuid4()}")
    user = await store.resolve_user(principal, uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", "chat-approve")
    run = AgentRunStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        agent="agent-notes",
        model="chat-fast",
        stream=False,
        new_messages=[{"role": "user", "content": "Сохрани"}],
    )
    await store.start_agent_run(run)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15)
    outcome = RunOutcome(
        status="awaiting_approval",
        text="Нужно подтверждение",
        steps=[StepRecord(status="ok", http_status=200, prompt_tokens=5, completion_tokens=1)],
        pending=[PendingCall("c1", "mcp-demo.create_note", {"text": "хлеб"})],
        state={"messages": [{"kind": "request"}]},
    )
    await store.finish_agent_run(run, outcome, Approval(code="K7Q2", expires_at=expires))
    found = await store.find_pending_run(user.id, conversation_id=conv)
    assert found is not None
    assert (found.id, found.code, found.agent) == (run.id, "K7Q2", "agent-notes")
    assert found.pending == [PendingCall("c1", "mcp-demo.create_note", {"text": "хлеб"})]
    assert found.state == {"messages": [{"kind": "request"}]}
    assert await store.find_pending_run(uuid.uuid7(), run_id=run.id) is None

    rid = uuid.uuid7()
    first = await store.resolve_pending_run(
        run.id, user.id, "approved", client_id="open-webui", request_id=rid
    )
    second = await store.resolve_pending_run(
        run.id, user.id, "denied", client_id="open-webui", request_id=uuid.uuid7()
    )
    assert first is not None
    assert second is None
    # Продолжение получает состояние, а в БД его и аргументов больше нет (ADR-0012, п. 1).
    assert first.state == {"messages": [{"kind": "request"}]}
    assert first.pending[0].arguments == {"text": "хлеб"}
    async with app_engine.connect() as conn:
        left = (
            await conn.execute(
                select(agent_runs.c.state.is_(None).label("no_state"), agent_runs.c.pending).where(
                    agent_runs.c.id == run.id
                )
            )
        ).one()
    assert left.no_state
    assert left.pending == [{"call_id": "c1", "ref": "mcp-demo.create_note"}]
    assert await store.find_pending_run(user.id, conversation_id=conv) is None

    await store.record_tool_call(
        request_id=rid,
        user_id=user.id,
        client_id="open-webui",
        run_id=rid,
        agent="agent-notes",
        ref="mcp-demo.create_note",
        outcome="allow",
        details={"approved_by": str(user.id), "duration_ms": 3},
    )
    async with app_engine.connect() as conn:
        status = (
            await conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == run.id))
        ).scalar_one()
        audit = (
            await conn.execute(
                select(audit_events.c.action, audit_events.c.outcome, audit_events.c.resource_id)
                .where(audit_events.c.request_id == rid)
                .order_by(audit_events.c.id)
            )
        ).all()
    assert status == "approved"
    assert [(a.action, a.outcome) for a in audit] == [
        ("tool.approval", "allow"),
        ("tool.call", "allow"),
    ]
    assert audit[1].resource_id == "mcp-demo.create_note"


async def test_expire_pending_runs(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    principal = _principal(f"expire-{uuid.uuid4()}")
    user = await store.resolve_user(principal, uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", None)
    run = AgentRunStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        agent="agent-notes",
        model="chat-fast",
        stream=False,
    )
    await store.start_agent_run(run)
    outcome = RunOutcome(
        status="awaiting_approval",
        steps=[StepRecord(status="ok")],
        pending=[PendingCall("c1", "mcp-demo.create_note", {})],
        state={},
    )
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    await store.finish_agent_run(run, outcome, Approval(code="AAAA", expires_at=past))
    rid = uuid.uuid7()
    assert await store.expire_pending_runs(user.id, client_id="open-webui", request_id=rid) == 1
    assert await store.expire_pending_runs(user.id, client_id="open-webui", request_id=rid) == 0
    async with app_engine.connect() as conn:
        status = (
            await conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == run.id))
        ).scalar_one()
        decisions = (
            (
                await conn.execute(
                    select(audit_events.c.details).where(
                        audit_events.c.request_id == rid, audit_events.c.action == "tool.approval"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert status == "expired"
    assert [d["decision"] for d in decisions] == ["expired"]


async def test_conversation_class_only_grows(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    principal = _principal(f"class-{uuid.uuid4()}")
    user = await store.resolve_user(principal, uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", "chat-class")
    assert await store.conversation_class(conv) is None
    await store.raise_conversation_class(conv, "confidential")
    await store.raise_conversation_class(conv, "internal")
    assert await store.conversation_class(conv) == "confidential"
    await store.raise_conversation_class(conv, "restricted")
    assert await store.conversation_class(conv) == "restricted"


@pytest.mark.parametrize(
    "stmt",
    [
        "select content from messages",
        "select state from agent_runs",
        "select email from users",
        "delete from users",
        "update agent_runs set user_id = user_id",
        "delete from audit_events",
        "update audit_events set outcome = 'allow'",
    ],
)
async def test_worker_role_privileges(worker_engine: AsyncEngine, stmt: str) -> None:
    async with worker_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(stmt))


async def test_worker_closes_unfinished(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, worker_engine: AsyncEngine
) -> None:
    """Истёкшее ожидание, потерянные вызов, запуск и инструмент; свежие не трогаются."""
    store = PgStore(app_engine)
    user = await store.resolve_user(_principal(f"worker-{uuid.uuid4()}"), uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", None)

    def call() -> CallStart:
        return CallStart(
            id=uuid.uuid7(),
            user_id=user.id,
            conversation_id=conv,
            client_id="open-webui",
            model="chat-fast",
            stream=False,
        )

    def run() -> AgentRunStart:
        return AgentRunStart(
            id=uuid.uuid7(),
            user_id=user.id,
            conversation_id=conv,
            client_id="open-webui",
            agent="agent-notes",
            model="chat-fast",
            stream=False,
        )

    async def tool(call_id: uuid.UUID, *, started: bool) -> None:
        await store.record_tool_call(
            request_id=uuid.uuid7(),
            user_id=user.id,
            client_id="open-webui",
            run_id=uuid.uuid7(),
            agent="agent-notes",
            ref="mcp-demo.create_note",
            outcome="allow",
            details={},
            call_id=call_id,
            started=started,
        )

    lost_call, lost_run, waiting = call(), run(), run()
    await store.start_call(lost_call)
    for item in (lost_run, waiting):
        await store.start_agent_run(item)
    await store.finish_agent_run(
        waiting,
        RunOutcome(
            status="awaiting_approval",
            steps=[StepRecord(status="ok")],
            pending=[PendingCall("c1", "mcp-demo.create_note", {"text": "хлеб"})],
            state={"messages": []},
        ),
        Approval(code="AAAA", expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)),
    )
    lost_tool, done_tool = uuid.uuid7(), uuid.uuid7()
    await tool(lost_tool, started=True)
    await tool(done_tool, started=True)
    await tool(done_tool, started=False)

    hour = dt.timedelta(hours=1)
    first = await sweep(worker_engine, hour)
    assert (first.expired, first.model_calls, first.agent_runs, first.tool_calls) == (1, 0, 0, 0)
    async with owner_engine.begin() as conn:
        for table, row_id in (("model_calls", lost_call.id), ("agent_runs", lost_run.id)):
            await conn.execute(
                text(f"update {table} set started_at = now() - interval '2 hours' where id = :i"),  # noqa: S608
                {"i": row_id},
            )
    second = await sweep(worker_engine, hour)
    assert (second.model_calls, second.agent_runs) == (1, 1)
    # Аудит не изменить даже владельцем, поэтому порог вместо сдвига времени.
    third = await sweep(worker_engine, dt.timedelta(0), dt.timedelta(days=1))
    assert third.tool_calls == 1
    again = await sweep(worker_engine, dt.timedelta(0))
    assert (again.expired, again.tool_calls) == (0, 0)

    async with app_engine.connect() as conn:
        statuses = {
            r.id: (r.status, r.error, r.no_state)
            for r in (
                await conn.execute(
                    select(
                        agent_runs.c.id,
                        agent_runs.c.status,
                        agent_runs.c.error,
                        agent_runs.c.state.is_(None).label("no_state"),
                    ).where(agent_runs.c.user_id == user.id)
                )
            ).all()
        }
        events = (
            await conn.execute(
                select(audit_events).where(audit_events.c.actor_client == "platform-worker")
            )
        ).all()
    assert statuses[lost_run.id] == ("error", "abandoned", True)
    assert statuses[waiting.id] == ("expired", None, True)
    assert await store.last_run_expired(user.id, run_id=waiting.id)
    assert not await store.last_run_expired(user.id, run_id=lost_run.id)
    assert not await store.last_run_expired(uuid.uuid7(), run_id=waiting.id)
    by_action = {e.action: e for e in events if e.actor_user_id == user.id}
    assert by_action["model.call"].request_id == lost_call.id
    assert by_action["agent.run"].details["reason"] == "abandoned"
    assert by_action["tool.call.unknown"].call_id == lost_tool
    expired = [e for e in events if e.resource_id == str(waiting.id)]
    assert [(e.action, e.actor_user_id) for e in expired] == [("tool.approval", user.id)]


async def _seed_chat(
    admin: AsyncEngine, chat_id: str, user: str, created: int, updated: int
) -> None:
    async with admin.begin() as conn:
        await conn.execute(
            text(
                "insert into chat (id, user_id, title, created_at, updated_at, chat) "
                "values (:i, :u, 't', extract(epoch from now())::bigint - :c * 86400, "
                "extract(epoch from now())::bigint - :p * 86400, '{}')"
            ),
            {"i": chat_id, "u": user, "c": created, "p": updated},
        )
        for stmt in (
            "insert into chat_message values (:i, :i, '{}')",
            "insert into shared_chat values (:i, :i)",
            "insert into automation_run values (:i, :i)",
            "insert into comment values (:i, 'chat', :i)",
            "insert into comment_reaction values (:i, :i)",
            "insert into notification values (:i, 'chat', :i)",
            "insert into chat_view_state values (:i, :i)",
            "insert into chatidtag values (:i, :i)",
        ):
            await conn.execute(text(stmt), {"i": chat_id})


async def _chat_rows(admin: AsyncEngine, table: str) -> list[str]:
    column = {"chat": "id", "comment": "resource_id", "notification": "resource_id"}.get(
        table, "comment_id" if table == "comment_reaction" else "chat_id"
    )
    async with admin.connect() as conn:
        rows = await conn.execute(
            text(f"select {column} from {table} where {column} is not null order by 1")  # noqa: S608
        )
        return list(rows.scalars())


_CHAT_TABLES = (
    "chat",
    "chat_message",
    "shared_chat",
    "automation_run",
    "comment",
    "comment_reaction",
    "notification",
    "chat_view_state",
    "chatidtag",
)


async def test_retention_purge(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    worker_engine: AsyncEngine,
    open_webui: tuple[AsyncEngine, AsyncEngine, AsyncEngine],
) -> None:
    admin, owui_worker, _ = open_webui
    await _seed_chat(admin, "idle", "u1", 100, 91)
    await _seed_chat(admin, "ancient", "u1", 181, 1)
    await _seed_chat(admin, "active", "u1", 120, 1)
    store = PgStore(app_engine)
    user = await store.resolve_user(_principal(f"purge-{uuid.uuid4()}"), uuid.uuid7())

    async def history(chat: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        conv = await store.ensure_conversation(user.id, "open-webui", chat)
        run = AgentRunStart(
            id=uuid.uuid7(),
            user_id=user.id,
            conversation_id=conv,
            client_id="open-webui",
            agent="agent-notes",
            model="chat-fast",
            stream=False,
            new_messages=[{"role": "user", "content": "x"}],
        )
        await store.start_agent_run(run)
        await store.finish_agent_run(
            run, RunOutcome(status="ok", text="y", steps=[StepRecord(status="ok")])
        )
        child = replace(run, id=uuid.uuid7(), parent_run_id=run.id, new_messages=[])
        await store.start_agent_run(child)
        await store.finish_agent_run(child, RunOutcome(status="ok"))
        return conv, run.id, child.id

    old, stale, fresh = await history("old"), await history("stale"), await history("fresh")

    async def age(ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID], days: int) -> None:
        conv, *runs = ids
        async with owner_engine.begin() as conn:
            p = {"c": conv, "d": dt.timedelta(days=days)}
            for stmt in (
                "update messages set created_at = now() - cast(:d as interval) "
                "where conversation_id = :c",
                "update model_calls set started_at = now() - cast(:d as interval) "
                "where conversation_id = :c",
                "update conversations set updated_at = now() - cast(:d as interval) where id = :c",
            ):
                await conn.execute(text(stmt), p)
            # Продолжение моложе границы: ссылка на удалённый запуск становится пустой.
            await conn.execute(
                text(
                    "update agent_runs set started_at = now() - cast(:d as interval) where id = :i"
                ),
                {"i": runs[0], "d": dt.timedelta(days=days)},
            )

    await age(old, 400)
    await age(stale, 100)
    counts = await purge(
        worker_engine, content_days=90, metadata_days=365, open_webui_engine=owui_worker
    )
    assert counts["messages"] == 4
    assert (counts["model_calls"], counts["agent_runs"]) == (1, 1)
    # У old остался свежий запуск-продолжение, поэтому разговор ещё нужен.
    assert counts["conversations"] == 0
    assert counts["open_webui_chats"] == 2
    for table in _CHAT_TABLES:
        assert await _chat_rows(admin, table) == ["active"], table
    async with app_engine.connect() as conn:
        left = set((await conn.execute(select(messages.c.conversation_id).distinct())).scalars())
        parent = (
            await conn.execute(select(agent_runs.c.parent_run_id).where(agent_runs.c.id == old[2]))
        ).scalar_one()
        event = (
            await conn.execute(
                select(audit_events).where(audit_events.c.action == "retention.purge")
            )
        ).one()
    assert fresh[0] in left
    assert not {old[0], stale[0]} & left
    assert parent is None
    assert event.outcome == "allow"
    assert event.details["deleted"]["messages"] == 4
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("update agent_runs set started_at = now() - interval '400 days' where id = :i"),
            {"i": old[2]},
        )
    assert (await purge(worker_engine, content_days=90, metadata_days=365))["conversations"] == 1


async def test_purge_failure_is_audited(
    worker_engine: AsyncEngine,
    app_engine: AsyncEngine,
    open_webui: tuple[AsyncEngine, AsyncEngine, AsyncEngine],
) -> None:
    admin, owui_worker, _ = open_webui
    async with admin.begin() as conn:
        await conn.execute(text("drop table chat cascade"))
    with pytest.raises(DBAPIError):
        await purge(
            worker_engine, content_days=90, metadata_days=365, open_webui_engine=owui_worker
        )
    async with app_engine.connect() as conn:
        outcomes = (
            await conn.execute(
                select(audit_events.c.outcome)
                .where(audit_events.c.action == "retention.purge")
                .order_by(audit_events.c.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert outcomes == "error"


async def test_subject_export_and_erase(
    app_engine: AsyncEngine,
    subject_engine: AsyncEngine,
    open_webui: tuple[AsyncEngine, AsyncEngine, AsyncEngine],
) -> None:
    admin, _, owui = open_webui
    store = PgStore(app_engine)
    oid = f"oid-{uuid.uuid4()}"
    principal = _principal(f"subject-{uuid.uuid4()}", oid)
    user = await store.resolve_user(principal, uuid.uuid7())
    other = await store.resolve_user(_principal(f"other-{uuid.uuid4()}"), uuid.uuid7())
    for who, chat in ((user, "mine"), (other, "theirs")):
        conv = await store.ensure_conversation(who.id, "open-webui", chat)
        call = CallStart(
            id=uuid.uuid7(),
            user_id=who.id,
            conversation_id=conv,
            client_id="open-webui",
            model="chat-fast",
            stream=False,
            new_messages=[{"role": "user", "content": "личное"}],
        )
        await store.start_call(call)
        await store.finish_call(call, CallResult(status="ok"))
    async with admin.begin() as conn:
        for owui_id, sub in (("w1", oid), ("w2", "another")):
            await conn.execute(
                text(
                    "insert into \"user\" values (:i, json_build_object('oidc', "
                    "json_build_object('sub', cast(:s as text))))"
                ),
                {"i": owui_id, "s": sub},
            )
    await _seed_chat(admin, "mine", "w1", 1, 1)
    await _seed_chat(admin, "theirs", "w2", 1, 1)

    data = await subject_data.export(subject_engine, user.id, open_webui_engine=owui)
    assert [m["content"]["content"] for m in data["messages"]] == ["личное"]
    assert [c["id"] for c in data["open_webui_chats"]] == ["mine"]
    assert data["user"][0]["email"] == "a@example.test"
    assert all(e["actor_user_id"] == user.id for e in data["audit_events"])

    counts = await subject_data.erase(subject_engine, user.id, open_webui_engine=owui)
    assert (counts["messages"], counts["identities"], counts["open_webui_chats"]) == (1, 1, 1)
    for table in _CHAT_TABLES:
        assert await _chat_rows(admin, table) == ["theirs"], table
    after = await subject_data.export(subject_engine, user.id)
    assert after["messages"] == after["identities"] == []
    row = after["user"][0]
    assert (row["display_name"], row["email"], row["status"]) == (None, None, "disabled")
    assert len(after["model_calls"]) == 1
    assert [c["external_id"] for c in after["conversations"]] == [None]
    actions = [e["action"] for e in after["audit_events"]]
    assert "subject.erase" in actions
    assert "subject.export" in actions
    theirs = await subject_data.export(subject_engine, other.id)
    assert len(theirs["messages"]) == 1
    # Повтор безопасен; без идентичности чаты можно найти только по явному antec_uid.
    with pytest.raises(subject_data.SubjectError, match="open-webui-uid"):
        await subject_data.erase(subject_engine, user.id, open_webui_engine=owui)
    again = await subject_data.erase(subject_engine, user.id)
    assert again["messages"] == 0
    # Новый вход того же человека = новый пользователь.
    assert (await store.resolve_user(principal, uuid.uuid7())).id != user.id
    with pytest.raises(subject_data.SubjectError, match="нет"):
        await subject_data.erase(subject_engine, uuid.uuid7())


async def test_step_rows_before_and_after(app_engine: AsyncEngine) -> None:
    """Шаг виден до вызова модели; итог с аудитом сразу, finish_agent_run его не дублирует."""
    store = PgStore(app_engine)
    user = await store.resolve_user(_principal(f"steps-{uuid.uuid4()}"), uuid.uuid7())
    conv = await store.ensure_conversation(user.id, "open-webui", None)
    run = AgentRunStart(
        id=uuid.uuid7(),
        user_id=user.id,
        conversation_id=conv,
        client_id="open-webui",
        agent="agent-notes",
        model="chat-fast",
        stream=False,
        new_messages=[{"role": "user", "content": "x"}],
    )
    await store.start_agent_run(run)
    saved = StepRecord(status="ok", prompt_tokens=3, id=uuid.uuid7())
    unsaved = StepRecord(status="error", id=uuid.uuid7())
    for step in (saved, unsaved):
        assert step.id is not None
        await store.start_step(run, step.id)
    await store.finish_step(run, saved, 1)
    saved.saved = True
    await store.finish_agent_run(run, RunOutcome(status="ok", text="y", steps=[saved, unsaved]))
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                select(model_calls.c.id, model_calls.c.status).where(
                    model_calls.c.agent_run_id == run.id
                )
            )
        ).all()
        events = (
            await conn.execute(
                select(audit_events.c.details)
                .where(audit_events.c.request_id == run.id, audit_events.c.action == "model.call")
                .order_by(audit_events.c.id)
            )
        ).scalars()
        assert [e["model_call_id"] for e in events] == [str(saved.id), str(unsaved.id)]
    assert {r.id: r.status for r in rows} == {saved.id: "ok", unsaved.id: "error"}


async def test_one_result_per_tool_call(app_engine: AsyncEngine) -> None:
    store = PgStore(app_engine)
    call_id = uuid.uuid7()

    async def record() -> None:
        await store.record_tool_call(
            request_id=uuid.uuid7(),
            user_id=uuid.uuid7(),
            client_id="open-webui",
            run_id=uuid.uuid7(),
            agent="agent-notes",
            ref="mcp-demo.get_time",
            outcome="allow",
            details={},
            call_id=call_id,
        )

    await record()
    with pytest.raises(DBAPIError):
        await record()


async def test_sql_error_hides_parameters(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as info:
            await conn.execute(text("insert into nope values (:a)"), {"a": "СЕКРЕТ"})
    assert "СЕКРЕТ" not in str(info.value)
