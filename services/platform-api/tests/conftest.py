"""Общие фикстуры: ключи и токены как у Keycloak, JWKS, подставные хранилище и шлюз."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from antec_platform.adapters.gateway import GatewayMeta, GatewayResponse, GatewayUnavailable
from antec_platform.agents import (
    AgentDefinition,
    Resume,
    RunOutcome,
    StepRecord,
    ToolSession,
    Turn,
)
from antec_platform.app import Deps, create_app
from antec_platform.auth import JwksCache, Principal, TokenVerifier
from antec_platform.policy import PolicyUnavailable, Resource, Subject
from antec_platform.store import (
    AgentRunStart,
    Approval,
    CallResult,
    CallStart,
    PendingRun,
    User,
)

ISSUER = "http://localhost:8080/realms/antec"
JWKS_URL = "http://keycloak:8080/realms/antec/protocol/openid-connect/certs"
KID = "test-kid"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class Keys:
    private: rsa.RSAPrivateKey
    kid: str = KID

    def jwk(self) -> dict[str, Any]:
        pub = self.private.public_key().public_numbers()
        return {
            "kid": self.kid,
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "n": _b64(pub.n),
            "e": _b64(pub.e),
        }


@pytest.fixture(scope="session")
def keys() -> Keys:
    return Keys(rsa.generate_private_key(public_exponent=65537, key_size=2048))


TokenFactory = Callable[..., str]


@pytest.fixture
def make_token(keys: Keys) -> TokenFactory:
    def factory(
        *,
        sub: str = "sub-1",
        aud: str | list[str] = "platform-api",
        azp: str = "open-webui",
        iss: str = ISSUER,
        exp_in: int = 300,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        **extra: object,
    ) -> str:
        now = int(time.time())
        claims: dict[str, object] = {
            "iss": iss,
            "sub": sub,
            "aud": aud,
            "azp": azp,
            "typ": "Bearer",
            "iat": now,
            "exp": now + exp_in,
            "name": "Тест Пользователь",
            "email": "user@example.test",
            "idp": "entra",
            "entra_tid": "tid-1",
            "entra_oid": f"oid-{sub}",
            "platform_roles": ["platform-user"],
            **extra,
        }
        return jwt.encode(
            claims, key or keys.private, algorithm="RS256", headers={"kid": kid or keys.kid}
        )

    return factory


@dataclass
class JwksServer:
    keys: list[dict[str, Any]]
    calls: int = 0
    fail: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        self.calls += 1
        if self.fail:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": self.keys})


@pytest.fixture
def jwks_server(keys: Keys) -> JwksServer:
    return JwksServer(keys=[keys.jwk()])


@pytest.fixture
def jwks(jwks_server: JwksServer) -> JwksCache:
    client = httpx.AsyncClient(transport=httpx.MockTransport(jwks_server.handler))
    return JwksCache(JWKS_URL, client, ttl=600)


@pytest.fixture
def verifier(jwks: JwksCache) -> TokenVerifier:
    return TokenVerifier(
        jwks=jwks,
        issuer=ISSUER,
        audience="platform-api",
        allowed_clients=["open-webui", "antec-smoke"],
        leeway=30,
    )


@dataclass
class FakeStore:
    users: dict[tuple[str, str], User] = field(default_factory=dict)
    denied: list[tuple[str, str]] = field(default_factory=list)
    conversations: dict[tuple[uuid.UUID, str, str | None], uuid.UUID] = field(default_factory=dict)
    started: list[CallStart] = field(default_factory=list)
    policy_denied: list[tuple[str, str, str]] = field(default_factory=list)
    deny_reasons: list[str] = field(default_factory=list)
    conv_classes: dict[uuid.UUID, str] = field(default_factory=dict)
    finished: list[tuple[CallStart, CallResult]] = field(default_factory=list)
    disabled: set[str] = field(default_factory=set)
    runs_started: list[AgentRunStart] = field(default_factory=list)
    runs_finished: list[tuple[AgentRunStart, RunOutcome]] = field(default_factory=list)
    approvals: list[Approval | None] = field(default_factory=list)
    pending: dict[uuid.UUID, tuple[uuid.UUID, PendingRun]] = field(default_factory=dict)
    resolved: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    tool_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    tool_starts: list[tuple[str, uuid.UUID | None]] = field(default_factory=list)
    tool_call_ids: list[uuid.UUID | None] = field(default_factory=list)
    audit_down: bool = False

    async def ping(self) -> None:
        return None

    async def resolve_user(self, principal: Principal, request_id: uuid.UUID) -> User:
        key = (principal.issuer, principal.subject)
        if key not in self.users:
            self.users[key] = User(id=uuid.uuid7(), status="active")
        user = self.users[key]
        status = "disabled" if principal.subject in self.disabled else user.status
        return User(id=user.id, status=status)

    async def record_denied(self, principal: Principal, reason: str, request_id: uuid.UUID) -> None:
        self.denied.append((principal.subject, reason))

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
        self.policy_denied.append((action, resource_type, resource_id))
        self.deny_reasons.append(reason)

    async def ensure_conversation(
        self, user_id: uuid.UUID, client_id: str, external_id: str | None
    ) -> uuid.UUID:
        if external_id is None:
            return uuid.uuid7()
        return self.conversations.setdefault((user_id, client_id, external_id), uuid.uuid7())

    async def start_call(self, call: CallStart) -> None:
        self.started.append(call)

    async def finish_call(self, call: CallStart, result: CallResult) -> None:
        self.finished.append((call, result))

    async def start_agent_run(self, run: AgentRunStart) -> None:
        self.runs_started.append(run)

    async def finish_agent_run(
        self, run: AgentRunStart, outcome: RunOutcome, approval: Approval | None = None
    ) -> None:
        self.runs_finished.append((run, outcome))
        self.approvals.append(approval)
        if outcome.status == "awaiting_approval" and approval is not None:
            self.pending[run.id] = (
                run.user_id,
                PendingRun(
                    id=run.id,
                    conversation_id=run.conversation_id,
                    agent=run.agent,
                    code=approval.code,
                    expires_at=approval.expires_at,
                    state=outcome.state or {},
                    pending=list(outcome.pending),
                ),
            )

    async def find_pending_run(
        self,
        user_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> PendingRun | None:
        for owner, run in reversed(self.pending.values()):
            if owner != user_id:
                continue
            if conversation_id is not None and run.conversation_id != conversation_id:
                continue
            if run_id is not None and run.id != run_id:
                continue
            return run
        return None

    async def last_run_expired(
        self,
        user_id: uuid.UUID,
        *,
        conversation_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> bool:
        runs = [
            r
            for r, _ in self.runs_finished
            if r.user_id == user_id
            and (conversation_id is None or r.conversation_id == conversation_id)
            and (run_id is None or r.id == run_id)
        ]
        return bool(runs) and (runs[-1].id, "expired") in self.resolved

    async def resolve_pending_run(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        status: str,
        *,
        client_id: str,
        request_id: uuid.UUID,
    ) -> PendingRun | None:
        item = self.pending.get(run_id)
        if item is None or item[0] != user_id:
            return None
        del self.pending[run_id]
        self.resolved.append((run_id, status))
        return item[1]

    async def conversation_class(self, conversation_id: uuid.UUID) -> str | None:
        return self.conv_classes.get(conversation_id)

    async def raise_conversation_class(self, conversation_id: uuid.UUID, data_class: str) -> None:
        order = ("public", "internal", "confidential", "restricted")
        current = self.conv_classes.get(conversation_id)
        if current is None or order.index(current) < order.index(data_class):
            self.conv_classes[conversation_id] = data_class

    async def expire_pending_runs(
        self, user_id: uuid.UUID, *, client_id: str, request_id: uuid.UUID
    ) -> int:
        now = dt.datetime.now(dt.UTC)
        stale = [
            k
            for k, (owner, run) in self.pending.items()
            if owner == user_id and run.expires_at <= now
        ]
        for key in stale:
            del self.pending[key]
            self.resolved.append((key, "expired"))
        return len(stale)

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
        if self.audit_down:
            raise RuntimeError("нет БД")
        if started:
            self.tool_starts.append((ref, call_id))
            return
        self.tool_calls.append((ref, outcome, details))
        self.tool_call_ids.append(call_id)


@dataclass
class FakeRun:
    deltas_value: list[str]
    result: RunOutcome
    hold: asyncio.Event | None = None
    cancelled: bool = False

    async def deltas(self) -> AsyncIterator[str]:
        for d in self.deltas_value:
            yield d
        if self.hold is not None:
            await self.hold.wait()

    async def outcome(self) -> RunOutcome:
        if self.cancelled:
            self.result.status = "cancelled"
        return self.result

    def cancel(self) -> None:
        self.cancelled = True
        if self.hold is not None:
            self.hold.set()


@dataclass
class FakeRuntime:
    """Рантайм для тестов фасада: заранее заданные фрагменты и итог."""

    deltas: list[str] = field(default_factory=lambda: ["При", "вет"])
    result: RunOutcome = field(
        default_factory=lambda: RunOutcome(
            status="ok",
            text="Привет",
            steps=[StepRecord(status="ok", http_status=200, prompt_tokens=7, completion_tokens=2)],
        )
    )
    hold: asyncio.Event | None = None
    started: list[tuple[AgentDefinition, str, list[Turn], str | None]] = field(default_factory=list)
    runs: list[FakeRun] = field(default_factory=list)
    resumes: list[Resume | None] = field(default_factory=list)
    sessions: list[ToolSession | None] = field(default_factory=list)

    def start(
        self,
        agent: AgentDefinition,
        user_id: str,
        history: list[Turn],
        prompt: str | None,
        tools: ToolSession | None = None,
        resume: Resume | None = None,
    ) -> FakeRun:
        self.started.append((agent, user_id, history, prompt))
        self.resumes.append(resume)
        self.sessions.append(tools)
        run = FakeRun(list(self.deltas), self.result, self.hold)
        self.runs.append(run)
        return run

    async def aclose(self) -> None:
        return None


ASSISTANT = AgentDefinition(
    name="agent-assistant",
    description="",
    model="chat-fast",
    instructions="Ты ассистент.",
    tools=(),
    max_steps=5,
    max_tool_calls=0,
)


@dataclass
class FakePolicy:
    """PDP для тестов: разрешено (роль, модель) из allowed; down = PDP недоступен."""

    allowed: set[tuple[str, str]] = field(
        default_factory=lambda: {("platform-user", "chat-default"), ("platform-user", "chat-fast")}
    )
    down: bool = False
    calls: list[tuple[Subject, str, list[Resource]]] = field(default_factory=list)

    async def check(self, subject: Subject, action: str, resources: list[Resource]) -> list[bool]:
        self.calls.append((subject, action, resources))
        if self.down:
            raise PolicyUnavailable("PDP недоступен")
        return [
            action == "invoke" and any((role, r.id) in self.allowed for role in subject.roles)
            for r in resources
        ]

    async def ping(self) -> None:
        if self.down:
            raise PolicyUnavailable("PDP недоступен")

    async def aclose(self) -> None:
        return None


@dataclass
class FakeStream:
    meta: GatewayMeta
    chunks: list[bytes]
    error: Any = None
    raise_after: int | None = None

    async def error_body(self) -> Any:  # noqa: ANN401
        return self.error

    async def events(self) -> AsyncIterator[bytes]:
        for i, chunk in enumerate(self.chunks):
            if self.raise_after is not None and i == self.raise_after:
                raise httpx.ReadError("обрыв")
            yield chunk


@dataclass
class FakeGateway:
    response: GatewayResponse | None = None
    stream_value: FakeStream | None = None
    unavailable: GatewayUnavailable | None = None
    payloads: list[dict[str, Any]] = field(default_factory=list)
    stream_closed: int = 0

    async def complete(self, payload: dict[str, Any]) -> GatewayResponse:
        self.payloads.append(payload)
        if self.unavailable is not None:
            raise self.unavailable
        assert self.response is not None
        return self.response

    @asynccontextmanager
    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[FakeStream]:
        self.payloads.append(payload)
        if self.unavailable is not None:
            raise self.unavailable
        assert self.stream_value is not None
        try:
            yield self.stream_value
        finally:
            self.stream_closed += 1

    async def aclose(self) -> None:
        return None


def ok_meta(cost: str | None = "0.0012") -> GatewayMeta:
    return GatewayMeta(
        status=200, request_id="call-1", cost_usd=Decimal(cost) if cost is not None else None
    )


@dataclass
class Harness:
    client: httpx.AsyncClient
    store: FakeStore
    gateway: FakeGateway
    policy: FakePolicy
    deps: Deps
    runtime: FakeRuntime


@pytest.fixture
async def harness(verifier: TokenVerifier, jwks: JwksCache) -> AsyncIterator[Harness]:
    store = FakeStore()
    gateway = FakeGateway()
    policy = FakePolicy()
    policy.allowed.add(("platform-user", ASSISTANT.name))
    runtime = FakeRuntime()
    deps = Deps(
        store=store,
        gateway=gateway,
        verifier=verifier,
        jwks=jwks,
        policy=policy,
        models=["chat-default", "chat-fast"],
        max_request_bytes=64 * 1024,
        agents={ASSISTANT.name: ASSISTANT},
        runtime=runtime,
    )

    async def factory() -> Deps:
        return deps

    app = create_app(factory)
    app.state.deps = deps  # ASGITransport не выполняет lifespan
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform-api") as client:
        yield Harness(
            client=client,
            store=store,
            gateway=gateway,
            policy=policy,
            deps=deps,
            runtime=runtime,
        )


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("PLATFORM_"):
            monkeypatch.delenv(key)
