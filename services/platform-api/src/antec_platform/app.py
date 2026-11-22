"""Приложение FastAPI: OpenAI-совместимый фасад Platform API (ADR-0006, п. 2)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from antec_platform.errors import ApiError, error_body, forbidden, unauthorized
from antec_platform.policy import PolicyClient, PolicyUnavailable, Resource, Subject
from antec_platform.store import CallResult, CallStart, Store, User

log = logging.getLogger(__name__)

# Идентификатор чата клиента (Open WebUI: X-OpenWebUI-Chat-Id). Значение утверждает клиент,
# поэтому оно действует только внутри пользователя и клиента (ADR-0006, п. 6).
CHAT_ID_HEADER = "x-openwebui-chat-id"
_EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_ERROR_MESSAGE_LIMIT = 500
_PROBES = frozenset({"/healthz", "/readyz"})


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


async def authorize_model(request: Request, principal: Principal, user: User, model: str) -> None:
    deps = _deps(request)
    (allowed,) = await _check(
        deps, _subject(principal, user), INVOKE, [Resource(MODEL_KIND, model)]
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
        await read_json(request, deps.max_request_bytes), frozenset(deps.models)
    )
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
    """Только модели, на которые у пользователя есть право invoke (одна пакетная проверка)."""
    principal, user = await authenticate(request)
    deps = _deps(request)
    decisions = await _check(
        deps, _subject(principal, user), INVOKE, [Resource(MODEL_KIND, m) for m in deps.models]
    )
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "antec"}
                for m, ok in zip(deps.models, decisions, strict=True)
                if ok
            ],
        }
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
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.add_middleware(AccessLogMiddleware)
    return app
