"""Приложение FastAPI: OpenAI-совместимый фасад Platform API (ADR-0006, п. 2)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from antec_platform.adapters.gateway import ChatGateway, GatewayMeta, GatewayUnavailable
from antec_platform.agents import (
    AGENT_KIND,
    AgentDefinition,
    AgentRun,
    AgentRuntime,
    Resume,
    RunOutcome,
    Turn,
    agent_input,
)
from antec_platform.auth import (
    DeniedToken,
    InvalidToken,
    JwksCache,
    JwksUnavailable,
    Principal,
    TokenVerifier,
    bearer_token,
)
from antec_platform.chat import (
    ChatRequest,
    StreamAccumulator,
    parse_chat_request,
    response_id,
    response_message,
    usage_tokens,
)
from antec_platform.data_classes import DATA_CLASS_REASON, DEFAULT_CLEARANCE, allows
from antec_platform.errors import ApiError, error_body, forbidden, unauthorized
from antec_platform.policy import PolicyClient, PolicyUnavailable, Resource, Subject
from antec_platform.store import AgentRunStart, Approval, CallResult, CallStart, Store, User
from antec_platform.tools import McpClient, McpServerSpec, TokenExchanger, ToolBroker

log = logging.getLogger(__name__)

# Идентификатор чата клиента (Open WebUI: X-OpenWebUI-Chat-Id). Значение утверждает клиент,
# поэтому оно действует только внутри пользователя и клиента (ADR-0006, п. 6).
CHAT_ID_HEADER = "x-openwebui-chat-id"
_EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_ERROR_MESSAGE_LIMIT = 500
_PROBES = frozenset({"/healthz", "/readyz"})
# Подтверждение в чате (ADR-0010, п. 7): код без похожих символов (0/O, 1/I).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_APPROVE_RE = re.compile(r"подтверждаю\s+([A-Za-z0-9]{4,8})[.!]?", re.IGNORECASE)
_DENY_RE = re.compile(r"отмена[.!]?", re.IGNORECASE)
# Итоги запуска, на которые клиент получает ответ (а не ошибку).
_ANSWERED = frozenset({"ok", "awaiting_approval"})


@dataclass(slots=True)
class Deps:
    store: Store
    gateway: ChatGateway
    verifier: TokenVerifier
    jwks: JwksCache
    policy: PolicyClient
    models: list[str]
    max_request_bytes: int
    # Scope политик инсталляции (ADR-0008) для аудита отказов.
    policy_scope: str | None = None
    # Агенты инсталляции (ADR-0010) и рантайм; без агентов рантайма нет.
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    runtime: AgentRuntime | None = None
    # Инструменты агентов (ADR-0010, п. 6): MCP-серверы манифеста, транспорт, обмен токена.
    mcp_servers: dict[str, McpServerSpec] = field(default_factory=dict)
    mcp_client: McpClient | None = None
    token_exchange: TokenExchanger | None = None
    approval_ttl_seconds: int = 900
    # Классы данных (ADR-0011): допуски логических моделей и класс ввода пользователя.
    clearances: dict[str, str] = field(default_factory=dict)
    default_data_class: str = "internal"

    def clearance(self, model: str) -> str:
        return self.clearances.get(model, DEFAULT_CLEARANCE)

    def model_allowed(self, model: str) -> bool:
        """Инвариант ADR-0011: ввод пользователя (default_data_class) не выше допуска модели."""
        return allows(self.clearance(model), self.default_data_class)

    # Фоновые записи итогов вызова: переживают отмену запроса клиентом, ждутся при остановке.
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def _deps(request: Request) -> Deps:
    deps: Deps = request.app.state.deps
    return deps


def _request_id(request: Request) -> uuid.UUID:
    rid: uuid.UUID = request.state.request_id
    return rid


async def authenticate(request: Request) -> tuple[Principal, User]:
    deps = _deps(request)
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise unauthorized("нужен заголовок Authorization: Bearer <access token>")
    try:
        principal = await deps.verifier.verify(token)
    except InvalidToken as exc:
        log.info("токен отклонён: %s", exc)
        raise unauthorized() from exc
    except JwksUnavailable as exc:
        log.error("проверка токена невозможна: %s", exc)
        raise ApiError(
            503, "проверка токена временно недоступна", type_="api_error", code="jwks_unavailable"
        ) from exc
    except DeniedToken as exc:
        await deps.store.record_denied(exc.principal, exc.reason, _request_id(request))
        log.info("токен не для Platform API: %s azp=%s", exc.reason, exc.principal.client_id)
        raise forbidden("токен выдан не для Platform API или не разрешённому клиенту") from exc
    user = await deps.store.resolve_user(principal, _request_id(request))
    if user.status != "active":
        await deps.store.record_denied(principal, "disabled", _request_id(request))
        raise forbidden("пользователь заблокирован", code="user_disabled")
    request.state.user_id = user.id
    return principal, user


MODEL_KIND = "model"
INVOKE = "invoke"


def _subject(principal: Principal, user: User) -> Subject:
    attrs = {"client_id": principal.client_id}
    if principal.idp:
        attrs["idp"] = principal.idp
    return Subject(id=str(user.id), roles=principal.roles, attrs=attrs)


async def _check(
    deps: Deps, subject: Subject, action: str, resources: list[Resource]
) -> list[bool]:
    """Решения PDP; сбой PDP = 503, а не разрешение (ADR-0009, п. 5)."""
    try:
        return await deps.policy.check(subject, action, resources)
    except PolicyUnavailable as exc:
        log.error("проверка прав невозможна: %s", exc)
        raise ApiError(
            503, "проверка прав временно недоступна", type_="api_error", code="policy_unavailable"
        ) from exc


async def authorize_agent(
    request: Request, principal: Principal, user: User, agent: AgentDefinition
) -> None:
    """Агент и его модель (ADR-0010, п. 4): агент не получает модель, недоступную пользователю.

    Сначала инвариант класса данных (ADR-0011): модель агента допущена к вводу пользователя.
    """
    deps = _deps(request)
    if not deps.model_allowed(agent.model):
        raise await _deny_data_class(request, principal, user, AGENT_KIND, agent.name, agent.model)
    attrs = {"data_class": deps.default_data_class}
    agent_ok, model_ok = await _check(
        deps,
        _subject(principal, user),
        INVOKE,
        [Resource(AGENT_KIND, agent.name, attrs), Resource(MODEL_KIND, agent.model, attrs)],
    )
    for ok, kind, resource_id in (
        (agent_ok, AGENT_KIND, agent.name),
        (model_ok, MODEL_KIND, agent.model),
    ):
        if not ok:
            await deps.store.record_policy_deny(
                principal,
                user.id,
                INVOKE,
                kind,
                resource_id,
                _request_id(request),
                scope=deps.policy_scope,
                data_class=deps.default_data_class,
            )
    if not agent_ok:
        log.info("политика: нет права invoke на агента %s", agent.name)
        raise forbidden(f"нет доступа к агенту {agent.name!r}", code="agent_forbidden")
    if not model_ok:
        log.info("политика: агент %s, нет права invoke на модель %s", agent.name, agent.model)
        raise forbidden(
            f"нет доступа к модели {agent.model!r} агента {agent.name!r}", code="model_forbidden"
        )


async def _deny_data_class(
    request: Request, principal: Principal, user: User, kind: str, resource_id: str, model: str
) -> ApiError:
    deps = _deps(request)
    await deps.store.record_policy_deny(
        principal,
        user.id,
        INVOKE,
        kind,
        resource_id,
        _request_id(request),
        scope=deps.policy_scope,
        reason=DATA_CLASS_REASON,
        data_class=deps.default_data_class,
    )
    log.info("класс данных: %s выше допуска модели %s", deps.default_data_class, model)
    return forbidden(
        f"модель {model!r} не допущена к данным класса {deps.default_data_class!r}",
        code="data_class_forbidden",
    )


async def authorize_model(request: Request, principal: Principal, user: User, model: str) -> None:
    deps = _deps(request)
    if not deps.model_allowed(model):
        raise await _deny_data_class(request, principal, user, MODEL_KIND, model, model)
    (allowed,) = await _check(
        deps,
        _subject(principal, user),
        INVOKE,
        [Resource(MODEL_KIND, model, {"data_class": deps.default_data_class})],
    )
    if not allowed:
        await deps.store.record_policy_deny(
            principal,
            user.id,
            INVOKE,
            MODEL_KIND,
            model,
            _request_id(request),
            scope=deps.policy_scope,
            data_class=deps.default_data_class,
        )
        log.info("политика: нет права invoke на модель %s", model)
        raise forbidden(f"нет доступа к модели {model!r}", code="model_forbidden")


async def read_json(request: Request, limit: int) -> object:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise ApiError(413, f"тело запроса больше {limit} байт", code="request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ApiError(413, f"тело запроса больше {limit} байт", code="request_too_large")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ApiError(400, "тело запроса не JSON", code="invalid_json") from exc


def _upstream_error(meta: GatewayMeta, body: object) -> ApiError:
    """Ошибка шлюза для клиента: 4xx как есть (бюджет, параметры), 5xx = 502."""
    message, type_, code = "ошибка LLM-шлюза", "api_error", None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        if isinstance(err.get("message"), str):
            message = err["message"][:_ERROR_MESSAGE_LIMIT]
        if isinstance(err.get("type"), str):
            type_ = err["type"][:64]
        if isinstance(err.get("code"), (str, int)):
            code = str(err["code"])[:64]
    if meta.status in (401, 403, 407):
        # Отказ шлюза в доступе = ошибка ключа Platform API, а не пользователя: клиенту 502,
        # текст шлюза (о ключе) не показывается.
        log.error("LLM-шлюз отклонил ключ Platform API: HTTP %s", meta.status)
        return ApiError(
            502,
            "LLM-шлюз отклонил запрос Platform API",
            type_="api_error",
            code="upstream_auth_error",
        )
    if 400 <= meta.status < 500:
        return ApiError(meta.status, message, type_=type_, code=code)
    return ApiError(502, "LLM-шлюз вернул ошибку", type_="api_error", code="upstream_error")


def _external_id(request: Request) -> str | None:
    value = request.headers.get(CHAT_ID_HEADER)
    if value and _EXTERNAL_ID_RE.fullmatch(value):
        return value
    return None


async def _finish(deps: Deps, call: CallStart, result: CallResult) -> None:
    try:
        await deps.store.finish_call(call, result)
    except Exception:
        log.exception("не удалось записать итог вызова %s", call.id)


async def chat_completions(request: Request) -> Response:
    deps = _deps(request)
    principal, user = await authenticate(request)
    chat = parse_chat_request(
        await read_json(request, deps.max_request_bytes), frozenset([*deps.models, *deps.agents])
    )
    if chat.model in deps.agents:
        return await _agent_completion(request, deps, principal, user, chat)
    await authorize_model(request, principal, user, chat.model)
    conversation_id = await deps.store.ensure_conversation(
        user.id, principal.client_id, _external_id(request)
    )
    call = CallStart(
        id=_request_id(request),
        user_id=user.id,
        conversation_id=conversation_id,
        client_id=principal.client_id,
        model=chat.model,
        stream=chat.stream,
        new_messages=chat.new_messages(),
        data_class=deps.default_data_class,
    )
    await deps.store.start_call(call)
    payload = chat.upstream_payload(str(user.id))
    if chat.stream:
        return await _stream(deps, call, chat, payload)
    return await _complete(deps, call, payload)


async def _complete(deps: Deps, call: CallStart, payload: dict[str, Any]) -> Response:
    try:
        resp = await deps.gateway.complete(payload)
    except GatewayUnavailable as exc:
        await _finish(deps, call, CallResult(status="error", error=str(exc)))
        raise _gateway_down(exc) from exc
    meta = resp.meta
    if meta.status != 200:
        err = _upstream_error(meta, resp.body)
        status = "rejected" if 400 <= err.status < 500 else "error"
        await _finish(
            deps,
            call,
            CallResult(
                status=status,
                http_status=meta.status,
                gateway_request_id=meta.request_id,
                error=err.message,
            ),
        )
        raise err
    prompt, completion = usage_tokens(
        resp.body.get("usage") if isinstance(resp.body, dict) else None
    )
    await _finish(
        deps,
        call,
        CallResult(
            status="ok",
            http_status=200,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=meta.cost_usd,
            gateway_request_id=response_id(resp.body) or meta.request_id,
            response_message=response_message(resp.body),
        ),
    )
    return JSONResponse(resp.body)


def _gateway_down(exc: GatewayUnavailable) -> ApiError:
    if exc.timeout:
        return ApiError(
            504, "LLM-шлюз не ответил вовремя", type_="api_error", code="upstream_timeout"
        )
    return ApiError(502, "LLM-шлюз недоступен", type_="api_error", code="upstream_unavailable")


def _sse_error(message: str, code: str) -> bytes:
    return b"data: " + json.dumps(error_body(message, "api_error", code)).encode() + b"\n\n"


async def _stream(
    deps: Deps, call: CallStart, chat: ChatRequest, payload: dict[str, Any]
) -> Response:
    stack = AsyncExitStack()
    try:
        upstream = await stack.enter_async_context(deps.gateway.stream(payload))
    except GatewayUnavailable as exc:
        await stack.aclose()
        await _finish(deps, call, CallResult(status="error", error=str(exc)))
        raise _gateway_down(exc) from exc
    meta = upstream.meta
    if meta.status != 200:
        try:
            error_payload = await upstream.error_body()
        finally:
            await stack.aclose()
        err = _upstream_error(meta, error_payload)
        status = "rejected" if 400 <= err.status < 500 else "error"
        await _finish(
            deps,
            call,
            CallResult(
                status=status,
                http_status=meta.status,
                gateway_request_id=meta.request_id,
                error=err.message,
            ),
        )
        raise err

    acc = StreamAccumulator(client_wants_usage=chat.client_wants_usage)
    result = CallResult(status="cancelled", http_status=200, gateway_request_id=meta.request_id)
    finalized = False

    def finalize() -> None:
        """Итог вызова и закрытие потока шлюза ровно один раз, отдельной задачей.

        Вызывается из finally генератора и из _GuardedStream: если клиент ушёл до первой
        итерации, генератор не запускается и его finally не выполняется.
        """
        nonlocal finalized
        if finalized:
            return
        finalized = True
        result.prompt_tokens, result.completion_tokens = usage_tokens(acc.usage)
        result.cost_usd = meta.cost_usd
        result.gateway_request_id = acc.response_id or meta.request_id
        result.response_message = acc.message()
        deps.spawn(_close_and_finish(stack, deps, call, result))

    async def body() -> AsyncIterator[bytes]:
        try:
            async for event in upstream.events():
                out = acc.feed(event)
                if out is not None:
                    yield out
            result.status = "ok" if acc.done else "error"
            if not acc.done:
                result.error = "поток шлюза оборвался без [DONE]"
                yield _sse_error("ответ модели прерван", "upstream_incomplete")
        except (httpx.HTTPError, GatewayUnavailable) as exc:
            result.status = "error"
            result.error = f"поток шлюза прерван: {type(exc).__name__}"
            log.warning("поток шлюза прерван: %s", type(exc).__name__)
            yield _sse_error("ответ модели прерван", "upstream_interrupted")
        except Exception as exc:
            result.status = "error"
            result.error = f"ошибка обработки потока: {type(exc).__name__}"
            raise
        finally:
            # Отмена клиентом приходит сюда как CancelledError или GeneratorExit: статус остаётся
            # cancelled.
            finalize()

    return _GuardedStream(
        body(),
        finalize,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _GuardedStream(StreamingResponse):
    """Поток, который гарантированно вызывает finalize, даже если тело не начали читать."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        finalize: Callable[[], None],
        *,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._finalize = finalize

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finalize()


async def _close_and_finish(
    stack: AsyncExitStack, deps: Deps, call: CallStart, result: CallResult
) -> None:
    try:
        await stack.aclose()
    except Exception:
        log.warning("поток шлюза закрыт с ошибкой", exc_info=True)
    await _finish(deps, call, result)


async def list_models(request: Request) -> Response:
    """Модели и агенты с правом invoke (одна пакетная проверка).

    Агент виден, только если разрешены и он, и его модель (ADR-0010, п. 4); модель и агент
    с допуском ниже класса ввода не показываются (ADR-0011, п. 3).
    """
    principal, user = await authenticate(request)
    deps = _deps(request)
    models = [m for m in deps.models if deps.model_allowed(m)]
    agents = [a for a in deps.agents.values() if deps.model_allowed(a.model)]
    attrs = {"data_class": deps.default_data_class}
    decisions = await _check(
        deps,
        _subject(principal, user),
        INVOKE,
        [Resource(MODEL_KIND, m, attrs) for m in models]
        + [Resource(AGENT_KIND, a.name, attrs) for a in agents],
    )
    allowed_models = {m for m, ok in zip(models, decisions[: len(models)], strict=True) if ok}
    data: list[dict[str, Any]] = [
        {"id": m, "object": "model", "created": 0, "owned_by": "antec"}
        for m in models
        if m in allowed_models
    ]
    data += [
        {"id": a.name, "object": "model", "created": 0, "owned_by": "antec-agent"}
        for a, ok in zip(agents, decisions[len(models) :], strict=True)
        if ok and a.model in allowed_models
    ]
    return JSONResponse({"object": "list", "data": data})


def _agent_error(outcome: RunOutcome) -> ApiError:
    status = outcome.http_status or 502
    return ApiError(
        status,
        outcome.error or "ошибка агента",
        type_="invalid_request_error" if 400 <= status < 500 else "api_error",
        code=outcome.error_code or "agent_error",
    )


@dataclass(slots=True)
class _RunContext:
    """Запуск агента в рамках запроса: запись, код подтверждения, ответ клиенту."""

    deps: Deps
    run: AgentRunStart
    handle: AgentRun
    code: str
    chat_confirm: bool

    async def finish(self, outcome: RunOutcome) -> None:
        approval = None
        if outcome.status == "awaiting_approval":
            expires = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.deps.approval_ttl_seconds)
            approval = Approval(code=self.code, expires_at=expires)
        try:
            await self.deps.store.finish_agent_run(self.run, outcome, approval)
        except Exception:
            log.exception("не удалось записать итог запуска агента %s", self.run.id)

    async def finish_when_done(self) -> None:
        await self.finish(await self.handle.outcome())

    def approval_text(self, outcome: RunOutcome) -> str:
        """Запрос подтверждения (ADR-0010, п. 7): что именно будет сделано и как ответить."""
        lines = ["", "", "Нужно подтверждение:"]
        for call in outcome.pending:
            args = json.dumps(call.arguments, ensure_ascii=False)
            if len(args) > 300:
                args = args[:300] + "…"
            lines.append(f"- {call.ref}({args})")
        minutes = max(1, self.deps.approval_ttl_seconds // 60)
        if self.chat_confirm:
            lines.append(f"Ответьте «подтверждаю {self.code}» или «отмена». Срок {minutes} мин.")
        else:
            lines.append(
                f"Подтвердите через POST /v1/agent-runs/{self.run.id}/decision. Срок {minutes} мин."
            )
        return "\n".join(lines)


def _approval_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))


def _usage(outcome: RunOutcome) -> dict[str, int]:
    prompt = outcome.prompt_tokens or 0
    completion = outcome.completion_tokens or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _chunk(base: dict[str, Any], delta: dict[str, Any], finish: str | None = None) -> bytes:
    body = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return b"data: " + json.dumps(body, ensure_ascii=False).encode() + b"\n\n"


def _completion_body(base: dict[str, Any], text: str, usage: dict[str, int]) -> dict[str, Any]:
    return {
        **base,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def _static_reply(model: str, rid: uuid.UUID, text: str, stream: bool) -> Response:
    """Ответ без вызова модели (решение по подтверждению)."""
    base = {"id": f"chatcmpl-{rid.hex}", "created": int(time.time()), "model": model}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not stream:
        return JSONResponse(_completion_body(base, text, usage))
    chunk_base = {**base, "object": "chat.completion.chunk"}

    async def body() -> AsyncIterator[bytes]:
        yield _chunk(chunk_base, {"role": "assistant", "content": text})
        yield _chunk(chunk_base, {}, "stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


def _decision(prompt: str, code: str) -> str:
    """approve, deny, wrong_code или other: решает код Platform API, а не модель."""
    match = _APPROVE_RE.fullmatch(prompt.strip())
    if match:
        return "approve" if match.group(1).upper() == code.upper() else "wrong_code"
    if _DENY_RE.fullmatch(prompt.strip()):
        return "deny"
    return "other"


def _tool_session(
    request: Request,
    deps: Deps,
    principal: Principal,
    user: User,
    agent: AgentDefinition,
    token: str,
) -> ToolBroker | None:
    if not agent.tools:
        return None
    if deps.mcp_client is None or deps.token_exchange is None:
        raise ApiError(
            503, "инструменты агентов не настроены", type_="api_error", code="tools_unavailable"
        )
    return ToolBroker(
        servers=deps.mcp_servers,
        client=deps.mcp_client,
        exchanger=deps.token_exchange,
        policy=deps.policy,
        audit=deps.store,
        subject=_subject(principal, user),
        subject_token=token,
        user_id=user.id,
        client_id=principal.client_id,
        request_id=_request_id(request),
        agent=agent.name,
        clearance=deps.clearance(agent.model),
        default_class=deps.default_data_class,
    )


async def _agent_completion(
    request: Request, deps: Deps, principal: Principal, user: User, chat: ChatRequest
) -> Response:
    """Запуск агента через фасад (ADR-0010, п. 4): ответ в формате Chat Completions.

    Если в разговоре ждёт подтверждения запуск этого агента, сообщение пользователя сначала
    сравнивается с кодом (ADR-0010, п. 7): «подтверждаю <код>» продолжает запуск, «отмена»
    отклоняет вызовы, неверный код и истёкший срок отменяют ожидание без вызова модели, любое
    другое сообщение отменяет ожидание и обрабатывается как новый вопрос.
    """
    agent = deps.agents[chat.model]
    await authorize_agent(request, principal, user, agent)
    history, prompt = agent_input(chat.messages)
    external_id = _external_id(request)
    conversation_id = await deps.store.ensure_conversation(
        user.id, principal.client_id, external_id
    )
    rid = _request_id(request)
    resume: Resume | None = None
    parent: uuid.UUID | None = None
    if external_id is not None:
        pending = await deps.store.find_pending_run(user.id, conversation_id=conversation_id)
        if pending is not None:
            kind = _decision(prompt, pending.code)
            expired = pending.expires_at <= dt.datetime.now(dt.UTC)
            if expired or pending.agent != agent.name or kind in ("other", "wrong_code"):
                status = "expired" if expired else "denied"
                await deps.store.resolve_pending_run(
                    pending.id, user.id, status, client_id=principal.client_id, request_id=rid
                )
                if kind != "other" and pending.agent == agent.name:
                    text = (
                        "Срок подтверждения истёк, действие не выполнено."
                        if expired
                        else "Код подтверждения не совпадает, действие отменено."
                    )
                    return _static_reply(agent.name, rid, text, chat.stream)
            else:
                resolved = await deps.store.resolve_pending_run(
                    pending.id,
                    user.id,
                    "approved" if kind == "approve" else "denied",
                    client_id=principal.client_id,
                    request_id=rid,
                )
                if resolved is None:
                    raise ApiError(409, "решение уже принято", code="approval_conflict")
                resume = Resume(
                    state=resolved.state,
                    approved={c.call_id: kind == "approve" for c in resolved.pending},
                    decided_by=str(user.id),
                )
                parent = resolved.id
    # Остальные истёкшие ожидания пользователя (другие разговоры, без ответа) = expired.
    await deps.store.expire_pending_runs(user.id, client_id=principal.client_id, request_id=rid)
    return await _run_agent(
        request,
        deps,
        principal,
        user,
        agent,
        conversation_id=conversation_id,
        history=history,
        prompt=prompt,
        stream=chat.stream,
        wants_usage=chat.client_wants_usage,
        new_messages=[m for m in chat.new_messages() if m["role"] == "user"],
        resume=resume,
        parent=parent,
        chat_confirm=external_id is not None,
    )


async def _run_agent(
    request: Request,
    deps: Deps,
    principal: Principal,
    user: User,
    agent: AgentDefinition,
    *,
    conversation_id: uuid.UUID,
    history: list[Turn],
    prompt: str | None,
    stream: bool,
    wants_usage: bool,
    new_messages: list[dict[str, Any]],
    resume: Resume | None,
    parent: uuid.UUID | None,
    chat_confirm: bool,
) -> Response:
    if deps.runtime is None:
        raise ApiError(
            503, "агентный рантайм не запущен", type_="api_error", code="agent_unavailable"
        )
    token = bearer_token(request.headers.get("authorization")) or ""
    tools = _tool_session(request, deps, principal, user, agent, token)
    run = AgentRunStart(
        id=_request_id(request),
        user_id=user.id,
        conversation_id=conversation_id,
        client_id=principal.client_id,
        agent=agent.name,
        model=agent.model,
        stream=stream,
        # В историю то, что получил агент: системные сообщения и вызовы инструментов клиента
        # агенту не передаются (ADR-0010, п. 4).
        new_messages=new_messages,
        parent_run_id=parent,
        data_class=deps.default_data_class,
    )
    await deps.store.start_agent_run(run)
    handle = deps.runtime.start(agent, str(user.id), history, prompt, tools, resume)
    ctx = _RunContext(deps, run, handle, _approval_code(), chat_confirm)
    base = {"id": f"chatcmpl-{run.id.hex}", "created": int(time.time()), "model": agent.name}
    deltas = handle.deltas()
    first: str | None = None
    try:
        if stream:
            # Первый фрагмент до ответа: ошибка до начала текста (бюджет, шлюз) = HTTP-ошибка.
            first = await anext(deltas, None)
        else:
            async for _ in deltas:
                pass
    except BaseException:
        # Отмена запроса или сбой до ответа: запуск прерывается, итог пишется отдельной задачей.
        handle.cancel()
        deps.spawn(ctx.finish_when_done())
        raise
    if not stream or first is None:
        outcome = await handle.outcome()
        if outcome.status == "awaiting_approval" and not stream:
            outcome.text += ctx.approval_text(outcome)
        if outcome.status not in _ANSWERED or not stream:
            await ctx.finish(outcome)
        if outcome.status not in _ANSWERED:
            raise _agent_error(outcome)
        if not stream:
            return JSONResponse(_completion_body(base, outcome.text, _usage(outcome)))
    return _agent_stream(ctx, deltas, first, base, wants_usage)


def _agent_stream(
    ctx: _RunContext,
    deltas: AsyncIterator[str],
    first: str | None,
    base: dict[str, Any],
    wants_usage: bool,
) -> Response:
    chunk_base = {**base, "object": "chat.completion.chunk"}
    finalized = False
    completed = False
    handle = ctx.handle

    def finalize() -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True
        if not completed:
            # Клиент ушёл до конца: запуск прерывается; итог записывается, когда рантайм его
            # отдаст.
            handle.cancel()
        ctx.deps.spawn(ctx.finish_when_done())

    async def body() -> AsyncIterator[bytes]:
        nonlocal completed
        try:
            yield _chunk(chunk_base, {"role": "assistant", "content": ""})
            if first is not None:
                yield _chunk(chunk_base, {"content": first})
            async for delta in deltas:
                yield _chunk(chunk_base, {"content": delta})
            outcome = await handle.outcome()
            completed = True
            if outcome.status not in _ANSWERED:
                err = _agent_error(outcome)
                yield _sse_error(err.message, err.code or "agent_error")
                return
            if outcome.status == "awaiting_approval":
                prompt = ctx.approval_text(outcome)
                outcome.text += prompt
                yield _chunk(chunk_base, {"content": prompt})
            yield _chunk(chunk_base, {}, "stop")
            if wants_usage:
                usage = {**chunk_base, "choices": [], "usage": _usage(outcome)}
                yield b"data: " + json.dumps(usage).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
        finally:
            finalize()

    return _GuardedStream(
        body(),
        finalize,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def agent_run_decision(request: Request) -> Response:
    """Решение по ожидающему запуску для адаптеров каналов (ADR-0010, п. 7).

    `{"decision": "approve" | "deny"}` с токеном того же пользователя; продолжает запуск и
    возвращает chat.completion. Чужой или неизвестный запуск 404, истёкший или уже решённый 409.
    """
    deps = _deps(request)
    principal, user = await authenticate(request)
    try:
        run_id = uuid.UUID(request.path_params["run_id"])
    except ValueError as exc:
        raise ApiError(404, "запуск не найден", code="run_not_found") from exc
    body = await read_json(request, 4096)
    decision = body.get("decision") if isinstance(body, dict) else None
    if decision not in ("approve", "deny"):
        raise ApiError(400, "decision: approve или deny", code="invalid_request", param="decision")
    rid = _request_id(request)
    pending = await deps.store.find_pending_run(user.id, run_id=run_id)
    if pending is None or pending.agent not in deps.agents:
        raise ApiError(404, "ожидающий запуск не найден", code="run_not_found")
    if pending.expires_at <= dt.datetime.now(dt.UTC):
        await deps.store.resolve_pending_run(
            pending.id, user.id, "expired", client_id=principal.client_id, request_id=rid
        )
        raise ApiError(409, "срок подтверждения истёк", code="approval_expired")
    agent = deps.agents[pending.agent]
    await authorize_agent(request, principal, user, agent)
    resolved = await deps.store.resolve_pending_run(
        pending.id,
        user.id,
        "approved" if decision == "approve" else "denied",
        client_id=principal.client_id,
        request_id=rid,
    )
    if resolved is None:
        raise ApiError(409, "решение уже принято", code="approval_conflict")
    return await _run_agent(
        request,
        deps,
        principal,
        user,
        agent,
        conversation_id=resolved.conversation_id,
        history=[],
        prompt=None,
        stream=False,
        wants_usage=False,
        new_messages=[],
        resume=Resume(
            state=resolved.state,
            approved={c.call_id: decision == "approve" for c in resolved.pending},
            decided_by=str(user.id),
        ),
        parent=resolved.id,
        chat_confirm=False,
    )


async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def readyz(request: Request) -> Response:
    deps = _deps(request)
    problems: list[str] = []
    try:
        await asyncio.wait_for(deps.store.ping(), timeout=3)
    except Exception as exc:
        problems.append(f"db: {type(exc).__name__}")
    try:
        await deps.policy.ping()
    except PolicyUnavailable as exc:
        problems.append(f"policy: {exc}")
    if not deps.jwks.loaded:
        try:
            await deps.jwks.refresh()
        except JwksUnavailable as exc:
            problems.append(f"jwks: {exc}")
    if problems:
        return JSONResponse({"status": "unavailable", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ok"})


class AccessLogMiddleware:
    """Идентификатор запроса и строка журнала на каждый запрос (без тела, токенов и query)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid7()
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", str(request_id).encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            state = scope.get("state", {})
            # Проверки здоровья (каждые 10 с) в журнал только при отказе или на уровне DEBUG.
            quiet = scope.get("path") in _PROBES and status == 200
            log.log(
                logging.DEBUG if quiet else logging.INFO,
                "%s %s %s",
                scope.get("method"),
                scope.get("path"),
                status,
                extra={
                    "request_id": str(request_id),
                    "user_id": str(state["user_id"]) if state.get("user_id") else None,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "http_status": status,
                },
            )


def create_app(
    deps_factory: Callable[[], Awaitable[Deps]],
    shutdown: Callable[[Deps], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        deps = await deps_factory()
        app.state.deps = deps
        try:
            yield
        finally:
            await deps.drain()
            if shutdown is not None:
                await shutdown(deps)

    app = FastAPI(
        title="antec.ai Platform API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> Response:
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> Response:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(
            error_body(str(exc.detail), "invalid_request_error", code), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, _exc: RequestValidationError) -> Response:
        return JSONResponse(
            error_body("некорректный запрос", "invalid_request_error", "invalid_request"),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.error("необработанная ошибка: %s", type(exc).__name__, exc_info=exc)
        return JSONResponse(
            error_body("внутренняя ошибка Platform API", "api_error", "internal_error"),
            status_code=500,
            headers={"x-request-id": str(getattr(request.state, "request_id", ""))},
        )

    app.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])
    app.add_api_route("/v1/models", list_models, methods=["GET"])
    app.add_api_route("/v1/agent-runs/{run_id}/decision", agent_run_decision, methods=["POST"])
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.add_middleware(AccessLogMiddleware)
    return app
