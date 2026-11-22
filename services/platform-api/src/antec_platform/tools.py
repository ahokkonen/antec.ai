"""Инструменты агентов (ADR-0010, п. 6): MCP-серверы манифеста, права, обмен токена, аудит.

`ToolBroker` создаётся на каждый запрос и действует от имени его пользователя:
1. список инструментов агента = белый список манифеста ∩ `tools/list` сервера;
2. перед вызовом проверка в PDP: ресурс `tool`, id `<server>.<tool>`, действие `call`;
3. токен пользователя меняется на токен с аудиторией сервера (RFC 8693, ADR-0007);
4. вызов `tools/call` и событие аудита `tool.call` (без аргументов и результата: ADR-0012).

Транспорт MCP и обмен токена за контрактами `McpClient` и `TokenExchanger` (adapters/).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from antec_platform.agents import AgentDefinition
from antec_platform.data_classes import DATA_CLASS_REASON, allows, rank
from antec_platform.policy import PolicyClient, PolicyUnavailable, Resource, Subject

log = logging.getLogger(__name__)

TOOL_KIND = "tool"
# Текст модели при отказе (права или класс данных): причина только в аудите.
DENIED_TEXT = "вызов инструмента запрещён (права пользователя или класс данных)"
CALL = "call"
_MODEL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


class ToolConfigError(Exception):
    """mcp_servers.json не разобран или не согласован с агентами."""


class ToolError(Exception):
    """Инструмент не выполнен (сеть, протокол, обмен токена); текст безопасно показать модели."""


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    read_only: bool
    # Класс данных результата (ADR-0011); None = класс по умолчанию инсталляции.
    data_class: str | None = None


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    name: str
    url: str
    audience: str
    protocol_version: str
    tools: tuple[McpToolSpec, ...]

    def tool(self, name: str) -> McpToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Инструмент, который видит модель."""

    ref: str  # <server>.<tool>
    server: str
    tool: str
    model_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    text: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpClient(Protocol):
    async def list_tools(self, server: McpServerSpec, token: str) -> list[RemoteTool]: ...

    async def call_tool(
        self, server: McpServerSpec, token: str, name: str, arguments: dict[str, Any]
    ) -> ToolCallResult: ...

    async def aclose(self) -> None: ...


class TokenExchanger(Protocol):
    async def exchange(self, subject_token: str, audience: str) -> str: ...

    async def aclose(self) -> None: ...


class ToolAudit(Protocol):
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
    ) -> None: ...


def _data_class(value: object) -> str | None:
    if value is None:
        return None
    rank(str(value))
    return str(value)


def model_tool_name(server: str, tool: str) -> str:
    """Имя для модели: только [A-Za-z0-9_-], не длиннее 64 (ограничение API провайдеров)."""
    return _MODEL_NAME_RE.sub("_", f"{server}__{tool}")[:64]


def load_mcp_servers(
    path: Path | None, agents: dict[str, AgentDefinition]
) -> dict[str, McpServerSpec]:
    """MCP-серверы из mcp_servers.json (рендер манифеста); ссылки агентов должны совпасть."""
    servers: dict[str, McpServerSpec] = {}
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw:
                spec = McpServerSpec(
                    name=str(item["name"]),
                    url=str(item["url"]),
                    audience=str(item["audience"]),
                    protocol_version=str(item["protocol_version"]),
                    tools=tuple(
                        McpToolSpec(
                            name=str(t["name"]),
                            read_only=bool(t["read_only"]),
                            data_class=_data_class(t.get("data_class")),
                        )
                        for t in item["tools"]
                    ),
                )
                if spec.name in servers:
                    raise ToolConfigError(f"MCP-сервер {spec.name} повторяется")
                servers[spec.name] = spec
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ToolConfigError(f"{path}: {exc!r}") from exc
    for agent in agents.values():
        for ref in agent.tools:
            server, _, tool = ref.partition(".")
            if server not in servers or servers[server].tool(tool) is None:
                raise ToolConfigError(f"агент {agent.name}: инструмента {ref} нет в mcp_servers")
    for agent in agents.values():
        agent_names = [model_tool_name(*ref.split(".", 1)) for ref in agent.tools]
        if len(set(agent_names)) != len(agent_names):
            raise ToolConfigError(f"агент {agent.name}: имена инструментов для модели совпадают")
    return servers


class ToolBroker:
    """Инструменты одного запроса от имени его пользователя."""

    def __init__(
        self,
        *,
        servers: dict[str, McpServerSpec],
        client: McpClient,
        exchanger: TokenExchanger,
        policy: PolicyClient,
        audit: ToolAudit,
        subject: Subject,
        subject_token: str,
        user_id: uuid.UUID,
        client_id: str,
        request_id: uuid.UUID,
        agent: str,
        clearance: str = "restricted",
        default_class: str = "internal",
    ) -> None:
        self._clearance = clearance
        self._default_class = default_class
        self._servers = servers
        self._client = client
        self._exchanger = exchanger
        self._policy = policy
        self._audit = audit
        self._subject = subject
        self._subject_token = subject_token
        self._user_id = user_id
        self._client_id = client_id
        self._request_id = request_id
        self._agent = agent
        self.run_id = request_id

    async def _token(self, server: McpServerSpec) -> str:
        return await self._exchanger.exchange(self._subject_token, server.audience)

    async def definitions(self, agent: AgentDefinition) -> list[ToolDefinition]:
        """Инструменты агента, которые есть и в манифесте, и на сервере.

        Инструмент из манифеста, которого сервер не объявил, не предлагается модели; сервер
        недоступен = его инструментов нет в этом запуске (агент отвечает без них).
        """
        by_server: dict[str, list[str]] = {}
        for ref in agent.tools:
            server_ref, _, tool_ref = ref.partition(".")
            by_server.setdefault(server_ref, []).append(tool_ref)
        out: list[ToolDefinition] = []
        for server_name, wanted in by_server.items():
            server = self._servers[server_name]
            try:
                remote = await self._client.list_tools(server, await self._token(server))
            except ToolError as exc:
                log.warning("MCP-сервер %s: список инструментов недоступен: %s", server.name, exc)
                continue
            offered = {t.name: t for t in remote}
            for tool in wanted:
                spec, info = server.tool(tool), offered.get(tool)
                if spec is None or info is None:
                    log.warning("MCP-сервер %s не объявил инструмент %s", server.name, tool)
                    continue
                out.append(
                    ToolDefinition(
                        ref=f"{server.name}.{tool}",
                        server=server.name,
                        tool=tool,
                        model_name=model_tool_name(server.name, tool),
                        description=info.description,
                        input_schema=info.input_schema,
                        read_only=spec.read_only,
                    )
                )
        return out

    def data_class(self, ref: str) -> str:
        server, _, tool = ref.partition(".")
        spec = self._servers[server].tool(tool)
        return (spec.data_class if spec else None) or self._default_class

    async def authorize(self, ref: str, read_only: bool) -> bool:
        """Право call на инструмент (до запроса подтверждения и перед вызовом).

        Сначала инвариант класса данных (ADR-0011, п. 3): данные выше допуска модели агента не
        запрашиваются вовсе; затем политика (атрибут data_class для ужесточения).
        """
        data_class = self.data_class(ref)
        details: dict[str, Any] = {"read_only": read_only, "data_class": data_class}
        if not allows(self._clearance, data_class):
            await self._record(
                ref,
                "deny",
                {**details, "reason": DATA_CLASS_REASON, "model_clearance": self._clearance},
            )
            return False
        try:
            (allowed,) = await self._policy.check(
                self._subject,
                CALL,
                [Resource(TOOL_KIND, ref, {"data_class": data_class})],
            )
        except PolicyUnavailable as exc:
            raise ToolError("проверка прав временно недоступна") from exc
        if not allowed:
            await self._record(ref, "deny", {**details, "reason": "policy"})
        return allowed

    async def call(
        self, ref: str, read_only: bool, arguments: dict[str, Any], approved_by: str | None
    ) -> ToolCallResult:
        """Вызов инструмента; запрет политики и ошибки = результат с is_error для модели."""
        if not await self.authorize(ref, read_only):
            return ToolCallResult(DENIED_TEXT, is_error=True)
        server_name, _, tool = ref.partition(".")
        server = self._servers[server_name]
        started = time.monotonic()
        details: dict[str, Any] = {
            "read_only": read_only,
            "approved_by": approved_by,
            "data_class": self.data_class(ref),
        }
        try:
            result = await self._client.call_tool(
                server, await self._token(server), tool, arguments
            )
        except ToolError as exc:
            details |= {"error": str(exc), "duration_ms": _ms(started)}
            await self._record(ref, "error", details)
            return ToolCallResult(f"инструмент недоступен: {exc}", is_error=True)
        details |= {"is_error": result.is_error, "duration_ms": _ms(started)}
        await self._record(ref, "error" if result.is_error else "allow", details)
        return result

    async def _record(self, ref: str, outcome: str, details: dict[str, Any]) -> None:
        try:
            await self._audit.record_tool_call(
                request_id=self._request_id,
                user_id=self._user_id,
                client_id=self._client_id,
                run_id=self.run_id,
                agent=self._agent,
                ref=ref,
                outcome=outcome,
                details=details,
            )
        except Exception:
            log.exception("не удалось записать аудит инструмента %s", ref)


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
