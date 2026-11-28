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
import datetime as dt
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    Tool,
    ToolDenied,
)
from pydantic_ai.exceptions import (
    AgentRunError,
    ApprovalRequired,
    ModelAPIError,
    ModelHTTPError,
    RunTimedOut,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits

from antec_platform.agents import (
    AgentDefinition,
    PendingCall,
    Resume,
    RunOutcome,
    StepRecord,
    StepSink,
    ToolSession,
    Turn,
)
from antec_platform.tools import DENIED_TEXT, ToolDefinition, ToolError

log = logging.getLogger(__name__)

_ERROR_LIMIT = 500
_DONE = object()
# Записи итога шагов, начатые при отмене: ссылка не даёт сборщику мусора убрать задачу.
_PENDING_STEPS: set[asyncio.Task[None]] = set()


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
        self._agents: dict[str, Agent[None, str | DeferredToolRequests]] = {
            name: Agent(
                OpenAIChatModel(spec.model, provider=provider),
                # Вызов, ждущий подтверждения, завершает запуск результатом DeferredToolRequests.
                output_type=[str, DeferredToolRequests],
                instructions=spec.instructions,
                name=name,
                # Повтор при невалидном ответе модели тоже вызов модели: не больше одного.
                retries=1,
            )
            for name, spec in agents.items()
        }

    def start(
        self,
        agent: AgentDefinition,
        user_id: str,
        history: list[Turn],
        prompt: str | None,
        tools: ToolSession | None = None,
        resume: Resume | None = None,
        steps: StepSink | None = None,
    ) -> PydanticAIRun:
        return PydanticAIRun(
            self._agents[agent.name],
            agent,
            user_id,
            history,
            prompt,
            self._timeout,
            tools=tools,
            resume=resume,
            sink=steps,
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


def _step_status(http_status: int) -> str:
    return "rejected" if 400 <= http_status < 500 else "error"


class _AuditedModel(WrapperModel):
    """Модель одного запуска: каждый вызов записан до него, итог после (ADR-0012, п. 3)."""

    def __init__(self, wrapped: Model, steps: list[StepRecord], sink: StepSink | None) -> None:
        super().__init__(wrapped)
        self._steps = steps
        self._sink = sink

    @asynccontextmanager
    async def _step(self) -> AsyncIterator[StepRecord]:
        step = StepRecord(
            status="cancelled", id=await self._sink.begin() if self._sink is not None else None
        )
        step.started_at = dt.datetime.now(dt.UTC)
        self._steps.append(step)
        try:
            yield step
        except ModelHTTPError as exc:
            step.status, step.http_status = _step_status(exc.status_code), exc.status_code
            # Текст шлюза может содержать ввод: в БД только код (ADR-0012, п. 5).
            step.error = f"upstream_error: HTTP {exc.status_code}"
            raise
        except Exception as exc:
            if step.status != "ok":
                step.status, step.error = "error", type(exc).__name__
            raise
        finally:
            step.finished_at = dt.datetime.now(dt.UTC)
            if self._sink is not None:
                # Отмена запуска не должна оборвать запись итога.
                task = asyncio.create_task(self._sink.finish(step))
                _PENDING_STEPS.add(task)
                task.add_done_callback(_PENDING_STEPS.discard)
                await asyncio.shield(task)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self._step() as step:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
            _fill(step, response)
            return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with (
            self._step() as step,
            self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream,
        ):
            try:
                yield stream
            finally:
                _fill(step, stream.get())


def _fill(step: StepRecord, response: ModelResponse) -> None:
    """Итог из ответа: оборванный (state не complete) = cancelled, а не ok."""
    step.status = "ok" if response.state == "complete" else "cancelled"
    step.http_status = 200
    step.prompt_tokens = response.usage.input_tokens
    step.completion_tokens = response.usage.output_tokens
    step.gateway_request_id = (response.provider_response_id or "")[:128] or None


def _http_error_message(exc: ModelHTTPError) -> str:
    body = exc.body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return str(err["message"])[:_ERROR_LIMIT]
        if isinstance(body.get("message"), str):
            return str(body["message"])[:_ERROR_LIMIT]
    return f"LLM-шлюз вернул HTTP {exc.status_code}"


_DENIED = "Пользователь отклонил этот вызов инструмента; не повторяй его без новой просьбы."


def _tool(defn: ToolDefinition, session: ToolSession, approved_by: str | None) -> Tool[None]:
    """Инструмент MCP для модели: права, подтверждение и вызов через ToolSession.

    Не read_only: без подтверждения в этом запуске вызов не выполняется (ApprovalRequired),
    но сначала проверяется право: запрещённое подтверждать не просим.
    """

    async def run(ctx: RunContext[None], **arguments: Any) -> str:  # noqa: ANN401
        try:
            if not defn.read_only and not ctx.tool_call_approved:
                if not await session.authorize(defn.ref, defn.read_only):
                    return DENIED_TEXT
                raise ApprovalRequired(metadata={"ref": defn.ref})
            result = await session.call(
                defn.ref,
                defn.read_only,
                arguments,
                None if defn.read_only else approved_by,
            )
        except ToolError as exc:
            return f"Ошибка инструмента: {exc}"
        return f"Ошибка инструмента: {result.text}" if result.is_error else result.text

    return Tool.from_schema(
        run,
        name=defn.model_name,
        description=defn.description or defn.ref,
        json_schema=defn.input_schema,
        takes_ctx=True,
    )


def _agent_model(agent: Agent[None, str | DeferredToolRequests]) -> Model:
    if not isinstance(agent.model, Model):
        raise TypeError("модель агента задаётся при создании рантайма")
    return agent.model


class PydanticAIRun:
    def __init__(
        self,
        agent: Agent[None, str | DeferredToolRequests],
        spec: AgentDefinition,
        user_id: str,
        history: list[Turn],
        prompt: str | None,
        timeout: float,
        *,
        tools: ToolSession | None,
        resume: Resume | None,
        sink: StepSink | None = None,
    ) -> None:
        # Шаги по мере вызовов модели; итог запуска ссылается на этот же список.
        self._steps: list[StepRecord] = []
        self._sink = sink
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._result = RunOutcome(status="cancelled", steps=self._steps)
        self._task = asyncio.create_task(
            self._drive(agent, spec, user_id, _history(history), prompt, timeout, tools, resume)
        )
        # Конец хода при любом исходе, в том числе при отмене до первого шага задачи.
        self._task.add_done_callback(lambda _task: self._queue.put_nowait(_DONE))

    async def _drive(
        self,
        agent: Agent[None, str | DeferredToolRequests],
        spec: AgentDefinition,
        user_id: str,
        history: list[ModelMessage],
        prompt: str | None,
        limit_seconds: float,
        tools: ToolSession | None,
        resume: Resume | None,
    ) -> None:
        text: list[str] = []
        output: str | DeferredToolRequests | None = None
        all_messages: list[ModelMessage] = []
        refs: dict[str, str] = {}
        try:
            toolsets: list[FunctionToolset[None]] = []
            if tools is not None and spec.tools:
                defs = await tools.definitions(spec)
                refs = {d.model_name: d.ref for d in defs}
                approver = resume.decided_by if resume is not None else None
                toolsets.append(FunctionToolset([_tool(d, tools, approver) for d in defs]))
            deferred: DeferredToolResults | None = None
            if resume is not None:
                history = list(ModelMessagesTypeAdapter.validate_python(resume.state["messages"]))
                deferred = DeferredToolResults(
                    approvals={
                        call_id: True if ok else ToolDenied(_DENIED)
                        for call_id, ok in resume.approved.items()
                    }
                )
                prompt = None
            async with agent.run_stream_events(
                prompt,
                message_history=history,
                deferred_tool_results=deferred,
                toolsets=toolsets,
                model_settings=OpenAIChatModelSettings(openai_user=user_id),
                usage_limits=UsageLimits(
                    request_limit=spec.max_steps, tool_calls_limit=spec.max_tool_calls
                ),
                timeout=limit_seconds,
                model=_AuditedModel(_agent_model(agent), self._steps, self._sink),
            ) as events:
                async for event in events:
                    delta = _text_delta(event)
                    if delta:
                        text.append(delta)
                        self._queue.put_nowait(delta)
                    if isinstance(event, AgentRunResultEvent):
                        output = event.result.output
                        if isinstance(output, str):
                            text = [output]
                        all_messages = event.result.all_messages()
            if isinstance(output, DeferredToolRequests):
                self._result = RunOutcome(
                    status="awaiting_approval",
                    text="".join(text),
                    steps=self._steps,
                    pending=[
                        PendingCall(
                            call_id=call.tool_call_id,
                            ref=refs.get(call.tool_name, call.tool_name),
                            arguments=call.args_as_dict(),
                        )
                        for call in output.approvals
                    ],
                    state={
                        "messages": ModelMessagesTypeAdapter.dump_python(all_messages, mode="json")
                    },
                )
            else:
                self._result = RunOutcome(status="ok", text="".join(text), steps=self._steps)
        except asyncio.CancelledError:
            self._result = RunOutcome(status="cancelled", text="".join(text), steps=self._steps)
            raise
        except ModelHTTPError as exc:
            message = _http_error_message(exc)
            status = _step_status(exc.status_code)
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
                steps=self._steps,
                error=message,
                http_status=client_status,
                error_code=code,
            )
        except UsageLimitExceeded as exc:
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=self._steps,
                error=f"превышен лимит агента: {exc}"[:_ERROR_LIMIT],
                http_status=422,
                error_code="agent_limit_exceeded",
            )
        except RunTimedOut:
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=self._steps,
                error="агент не уложился во время запроса",
                http_status=504,
                error_code="agent_timeout",
            )
        except (ModelAPIError, AgentRunError) as exc:
            log.warning("запуск агента %s: %s", spec.name, type(exc).__name__)
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=self._steps,
                error=f"ошибка агента: {type(exc).__name__}",
                http_status=502,
                error_code="agent_error",
            )
        except Exception as exc:
            log.exception("запуск агента %s: необработанная ошибка", spec.name)
            self._result = RunOutcome(
                status="error",
                text="".join(text),
                steps=self._steps,
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


def instrument_agents() -> None:
    """OTel-телеметрия всех агентов без содержимого (ADR-0013, п. 2; ADR-0012, п. 6).

    Без промптов, ответов, аргументов и результатов инструментов, двоичных данных и параметров
    запроса модели (описания инструментов). Провайдеры берутся глобальные (adapters/otel.py).
    """
    Agent.instrument_all(
        InstrumentationSettings(
            include_content=False,
            include_binary_content=False,
            include_model_request_parameters=False,
        )
    )
