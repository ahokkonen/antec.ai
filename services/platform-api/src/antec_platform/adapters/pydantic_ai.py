"""AgentRuntime на Pydantic AI 2.68 (ADR-0010, п. 1). Единственное место с типами фреймворка.

Модель агента = логическая модель LiteLLM через OpenAI-совместимый API (тот же ключ и адрес, что
у ChatGateway), `user` = id пользователя Platform API, без повторов в SDK: повторы решает
вызывающий. Запуск идёт в отдельной задаче asyncio, ход отдаётся через очередь: контекст
`run_stream_events` (группы задач anyio) открывается и закрывается в одной задаче, а читать ход
может задача потока ответа Starlette.

Не используются: граф, capabilities, встроенные инструменты провайдеров, Logfire (ADR-0010).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelHTTPError,
    RunTimedOut,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from antec_platform.agents import AgentDefinition, RunOutcome, StepRecord, Turn

log = logging.getLogger(__name__)

_ERROR_LIMIT = 500
_DONE = object()


class PydanticAIRuntime:
    def __init__(
        self,
        agents: dict[str, AgentDefinition],
        gateway_url: str,
        api_key: str,
        timeout: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(timeout, connect=10.0),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        client = AsyncOpenAI(
            base_url=gateway_url, api_key=api_key, max_retries=0, http_client=self._http
        )
        provider = OpenAIProvider(openai_client=client)
        self._agents = {
            name: Agent(
                OpenAIChatModel(spec.model, provider=provider),
                instructions=spec.instructions,
                name=name,
                # Повтор при невалидном ответе модели тоже вызов модели: не больше одного.
                retries=1,
            )
            for name, spec in agents.items()
        }

    def start(
        self, agent: AgentDefinition, user_id: str, history: list[Turn], prompt: str
    ) -> PydanticAIRun:
        return PydanticAIRun(
            self._agents[agent.name], agent, user_id, history, prompt, self._timeout
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _history(turns: list[Turn]) -> list[ModelMessage]:
    out: list[ModelMessage] = []
    for turn in turns:
        if turn.role == "user":
            out.append(ModelRequest(parts=[UserPromptPart(content=turn.text)]))
        else:
            out.append(ModelResponse(parts=[TextPart(content=turn.text)]))
    return out


def _steps(messages: list[ModelMessage]) -> list[StepRecord]:
    """Шаги = ответы модели этого запуска (прерванный ответ без частей не считается)."""
    steps = []
    for msg in messages:
        if isinstance(msg, ModelResponse) and msg.parts:
            steps.append(
                StepRecord(
                    status="ok",
                    http_status=200,
                    prompt_tokens=msg.usage.input_tokens,
                    completion_tokens=msg.usage.output_tokens,
                    gateway_request_id=(msg.provider_response_id or "")[:128] or None,
                )
            )
    return steps


def _http_error_message(exc: ModelHTTPError) -> str:
    body = exc.body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return str(err["message"])[:_ERROR_LIMIT]
        if isinstance(body.get("message"), str):
            return str(body["message"])[:_ERROR_LIMIT]
    return f"LLM-шлюз вернул HTTP {exc.status_code}"


class PydanticAIRun:
    def __init__(
        self,
        agent: Agent[None, str],
        spec: AgentDefinition,
        user_id: str,
        history: list[Turn],
        prompt: str,
        timeout: float,
    ) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._result = RunOutcome(status="cancelled")
        self._task = asyncio.create_task(
            self._drive(agent, spec, user_id, _history(history), prompt, timeout)
        )
        # Конец хода при любом исходе, в том числе при отмене до первого шага задачи.
        self._task.add_done_callback(lambda _task: self._queue.put_nowait(_DONE))

    async def _drive(
        self,
        agent: Agent[None, str],
        spec: AgentDefinition,
        user_id: str,
        history: list[ModelMessage],
        prompt: str,
        limit_seconds: float,
    ) -> None:
        text: list[str] = []
        messages: list[ModelMessage] = []
        try:
            async with agent.run_stream_events(
                prompt,
                message_history=history,
                model_settings=OpenAIChatModelSettings(openai_user=user_id),
                usage_limits=UsageLimits(
                    request_limit=spec.max_steps, tool_calls_limit=spec.max_tool_calls
                ),
                timeout=limit_seconds,
            ) as events:
                try:
                    async for event in events:
                        delta = _text_delta(event)
                        if delta:
                            text.append(delta)
                            self._queue.put_nowait(delta)
                        if isinstance(event, AgentRunResultEvent):
                            text = [event.result.output]
                finally:
                    messages = list(events.new_messages())
            self._result = RunOutcome(status="ok", text="".join(text), steps=_steps(messages))
        except asyncio.CancelledError:
            self._result = RunOutcome(
                status="cancelled", text="".join(text), steps=_steps(messages)
            )
            raise
        except ModelHTTPError as exc:
            message = _http_error_message(exc)
            status = "rejected" if 400 <= exc.status_code < 500 else "error"
            steps = _steps(messages)
            steps.append(StepRecord(status=status, http_status=exc.status_code, error=message))
            # Как у ChatGateway: 4xx шлюза (бюджет, параметры) клиенту как есть; отказ в ключе
            # Platform API (401, 403, 407) и 5xx = 502 без текста шлюза.
            client_status, code = exc.status_code, "upstream_error"
            if exc.status_code in (401, 403, 407):
                log.error("LLM-шлюз отклонил ключ Platform API: HTTP %s", exc.status_code)
                client_status, code, message = (
                    502,
                    "upstream_auth_error",
                    "LLM-шлюз отклонил запрос",
                )
            elif status == "error":
                client_status, message = 502, "LLM-шлюз вернул ошибку"
            self._result = RunOutcome(
                status=status,
                text="".join(text),
                steps=steps,
                error=message,
                http_status=client_status,
                error_code=code,
            )
        except UsageLimitExceeded as exc:
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=_steps(messages),
                error=f"превышен лимит агента: {exc}"[:_ERROR_LIMIT],
                http_status=422,
                error_code="agent_limit_exceeded",
            )
        except RunTimedOut:
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=_steps(messages),
                error="агент не уложился во время запроса",
                http_status=504,
                error_code="agent_timeout",
            )
        except (ModelAPIError, AgentRunError) as exc:
            log.warning("запуск агента %s: %s", spec.name, type(exc).__name__)
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=_steps(messages),
                error=f"ошибка агента: {type(exc).__name__}",
                http_status=502,
                error_code="agent_error",
            )
        except Exception as exc:
            log.exception("запуск агента %s: необработанная ошибка", spec.name)
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=_steps(messages),
                error=f"внутренняя ошибка агента: {type(exc).__name__}",
                http_status=500,
                error_code="internal_error",
            )

    async def deltas(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is _DONE:
                return
            yield item

    async def outcome(self) -> RunOutcome:
        # wait не поднимает исключение задачи и не глушит отмену вызывающего.
        if not self._task.done():
            await asyncio.wait({self._task})
        return self._result

    def cancel(self) -> None:
        self._task.cancel()


def _text_delta(event: object) -> str | None:
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content or None
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta or None
    return None
