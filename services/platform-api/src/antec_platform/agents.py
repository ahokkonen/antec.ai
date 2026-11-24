"""Контракт агентного рантайма (ADR-0010): описание агента, вход, ход запуска, итог.

Реализация (Pydantic AI) только в adapters/pydantic_ai.py; здесь и в остальном коде нет типов
фреймворка. Описания агентов приходят из манифеста инсталляции (`agents.json`, tools/instance).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from antec_platform.errors import ApiError

if TYPE_CHECKING:
    from antec_platform.tools import ToolCallResult, ToolDefinition

AGENT_PREFIX = "agent-"
AGENT_KIND = "agent"


class AgentConfigError(Exception):
    """agents.json не разобран или не согласован с моделями Platform API."""


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    description: str
    model: str
    instructions: str
    tools: tuple[str, ...]
    max_steps: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class Turn:
    """Текстовое сообщение истории разговора, переданной клиентом."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(slots=True)
class StepRecord:
    """Один вызов модели внутри запуска агента: строка model_calls."""

    status: str  # ok, error, rejected, cancelled
    http_status: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    gateway_request_id: str | None = None
    error: str | None = None
    # Время шага по рантайму; пусто = время записи итога.
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    # id строки model_calls, записанной до вызова (ADR-0012, п. 3), и записан ли уже итог.
    id: uuid.UUID | None = None
    saved: bool = False


@dataclass(frozen=True, slots=True)
class PendingCall:
    """Вызов инструмента, ждущий подтверждения человеком (ADR-0010, п. 7)."""

    call_id: str
    ref: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class RunOutcome:
    """Итог запуска: статус для agent_runs, текст ответа, шаги, ошибка для клиента.

    awaiting_approval: запуск остановлен до решения человека; `pending` = ожидающие вызовы,
    `state` = внутреннее состояние рантайма для продолжения (JSON, только для того же рантайма).
    """

    status: str  # ok, error, rejected, cancelled, awaiting_approval
    text: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    error: str | None = None
    # Ошибка для клиента (HTTP-статус и код OpenAI), если запуск не удался.
    http_status: int | None = None
    error_code: str | None = None
    pending: list[PendingCall] = field(default_factory=list)
    state: dict[str, Any] | None = None

    @property
    def prompt_tokens(self) -> int | None:
        return _sum(s.prompt_tokens for s in self.steps)

    @property
    def completion_tokens(self) -> int | None:
        return _sum(s.completion_tokens for s in self.steps)


def _sum(values: Iterable[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


class AgentRun(Protocol):
    """Запуск агента. Работает в своей задаче: читать ход можно из любой задачи."""

    def deltas(self) -> AsyncIterator[str]:
        """Фрагменты текста ответа по мере генерации; заканчивается вместе с запуском."""

    async def outcome(self) -> RunOutcome:
        """Итог (ждёт окончания запуска)."""

    def cancel(self) -> None:
        """Прервать запуск (клиент ушёл); итог получит статус cancelled."""


class ToolSession(Protocol):
    """Инструменты запуска от имени пользователя запроса (реализация: tools.ToolBroker)."""

    async def definitions(self, agent: AgentDefinition) -> list[ToolDefinition]: ...

    async def authorize(self, ref: str, read_only: bool) -> bool: ...

    async def call(
        self, ref: str, read_only: bool, arguments: dict[str, Any], approved_by: str | None
    ) -> ToolCallResult: ...


class StepSink(Protocol):
    """Запись шагов по мере выполнения (ADR-0012, п. 3): начало до вызова модели, итог после."""

    async def begin(self) -> uuid.UUID:
        """Строка model_calls до вызова. Исключение = модель не вызывается."""

    async def finish(self, step: StepRecord) -> None:
        """Итог шага; сбой записи не прерывает запуск (итог допишет finish_agent_run или worker)."""


@dataclass(frozen=True, slots=True)
class Resume:
    """Продолжение запуска после решения человека: состояние и решения по вызовам."""

    state: dict[str, Any]
    approved: dict[str, bool]
    decided_by: str


class AgentRuntime(Protocol):
    def start(
        self,
        agent: AgentDefinition,
        user_id: str,
        history: list[Turn],
        prompt: str | None,
        tools: ToolSession | None = None,
        resume: Resume | None = None,
        steps: StepSink | None = None,
    ) -> AgentRun: ...

    async def aclose(self) -> None: ...


def load_agents(path: Path | None, models: Iterable[str]) -> dict[str, AgentDefinition]:
    """Агенты из agents.json (рендер манифеста). Нет пути = агентов нет."""
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, list):
        raise AgentConfigError(f"{path}: ожидается список агентов")
    known = set(models)
    agents: dict[str, AgentDefinition] = {}
    for item in raw:
        agent = _definition(item)
        if agent.name in agents:
            raise AgentConfigError(f"агент {agent.name} повторяется")
        if agent.model not in known:
            raise AgentConfigError(
                f"агент {agent.name}: модели {agent.model} нет в PLATFORM_MODELS"
            )
        agents[agent.name] = agent
    return agents


def _definition(item: Any) -> AgentDefinition:  # noqa: ANN401 - сырые данные JSON
    try:
        agent = AgentDefinition(
            name=str(item["name"]),
            description=str(item.get("description") or ""),
            model=str(item["model"]),
            instructions=str(item["instructions"]),
            tools=tuple(str(t) for t in item.get("tools") or ()),
            max_steps=int(item["max_steps"]),
            max_tool_calls=int(item["max_tool_calls"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AgentConfigError(f"некорректное описание агента: {exc!r}") from exc
    if not agent.name.startswith(AGENT_PREFIX) or not agent.instructions.strip():
        raise AgentConfigError(f"некорректное описание агента {agent.name!r}")
    if not 1 <= agent.max_steps <= 50 or not 0 <= agent.max_tool_calls <= 100:
        raise AgentConfigError(f"агент {agent.name}: лимиты вне допустимых значений")
    return agent


def _text(content: object) -> str:
    """Текст сообщения OpenAI: строка или текстовые части списка (изображения не передаются)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def agent_input(messages: list[dict[str, Any]]) -> tuple[list[Turn], str]:
    """История и запрос для агента из сообщений клиента (ADR-0010, п. 4).

    Системные сообщения и вызовы инструментов клиента не передаются: инструкции агента задаёт
    манифест. Последнее сообщение должно быть от пользователя и содержать текст.
    """
    turns = [
        Turn(role=m["role"], text=text)
        for m in messages
        if m["role"] in ("user", "assistant") and (text := _text(m.get("content")))
    ]
    if not messages or messages[-1]["role"] != "user" or not turns or turns[-1].role != "user":
        raise ApiError(
            400,
            "последнее сообщение для агента должно быть текстом пользователя",
            code="invalid_request",
            param="messages",
        )
    return turns[:-1], turns[-1].text
