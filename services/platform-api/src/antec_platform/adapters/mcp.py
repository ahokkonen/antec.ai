"""McpClient: MCP 2026-07-28, Streamable HTTP без сессий (ADR-0010, п. 6).

Свой транспорт на httpx, типы сообщений из `mcp-types` (официальный пакет, только pydantic).
Каждый запрос: POST JSON-RPC с `Accept: application/json, text/event-stream`, заголовками
`MCP-Protocol-Version`, `Mcp-Method` (и `Mcp-Name` для tools/call) и `_meta` с версией и
возможностями клиента. Ответ: один JSON или поток SSE (берётся ответ с id запроса).
Ошибка инструмента (`isError`) возвращается модели как результат; ошибка протокола, HTTP или
сети = ToolError.
"""

from __future__ import annotations

import json
import uuid
from importlib import metadata
from typing import Any

import httpx
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
    CallToolResult,
    ListToolsResult,
    TextContent,
)
from pydantic import ValidationError

from antec_platform.tools import McpServerSpec, RemoteTool, ToolCallResult, ToolError

PROTOCOL_VERSION = "2026-07-28"
# Версия ядра из метаданных пакета (единый источник VERSION, ADR-0014).
CLIENT_INFO = {"name": "antec-platform-api", "version": metadata.version("antec-platform")}
_RESULT_LIMIT = 32 * 1024
# Тело ответа MCP-сервера читается потоком с ограничением: больше = ошибка инструмента.
MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PAGES = 10


class HttpMcpClient:
    def __init__(self, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _rpc(
        self,
        server: McpServerSpec,
        token: str,
        method: str,
        params: dict[str, Any],
        name: str | None = None,
    ) -> dict[str, Any]:
        if server.protocol_version != PROTOCOL_VERSION:
            raise ToolError(f"ревизия MCP {server.protocol_version} не поддерживается")
        rid = uuid.uuid4().hex
        body = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": {
                **params,
                "_meta": {
                    PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
                    CLIENT_CAPABILITIES_META_KEY: {},
                    CLIENT_INFO_META_KEY: CLIENT_INFO,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            if not name.isascii() or not name.isprintable():
                raise ToolError("имя инструмента не ASCII")
            headers["Mcp-Name"] = name
        try:
            async with self._http.stream("POST", server.url, json=body, headers=headers) as resp:
                status = resp.status_code
                ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                if status in (401, 403):
                    raise ToolError(f"MCP-сервер отклонил токен: HTTP {status}", "unauthorized")
                content = bytearray()
                async for chunk in resp.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ToolError(f"ответ MCP-сервера больше {MAX_RESPONSE_BYTES} байт")
        except httpx.TimeoutException as exc:
            raise ToolError("MCP-сервер не ответил вовремя", "timeout") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"MCP-сервер недоступен: {type(exc).__name__}", "unavailable") from exc
        message = _response_message(bytes(content), ctype, status, rid)
        if "error" in message:
            err = message["error"] if isinstance(message["error"], dict) else {}
            raise ToolError(
                f"MCP-ошибка {err.get('code')}: {str(err.get('message'))[:200]}", "mcp_error"
            )
        if status != 200:
            raise ToolError(f"MCP-сервер вернул HTTP {status}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ToolError("MCP-ответ без result")
        if result.get("resultType", "complete") != "complete":
            # input_required (MRTR, elicitation) в v1 не поддерживается.
            raise ToolError(
                f"MCP-сервер ждёт ввода ({result.get('resultType')}): не поддерживается"
            )
        return result

    async def list_tools(self, server: McpServerSpec, token: str) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            raw = await self._rpc(server, token, "tools/list", params)
            try:
                result = ListToolsResult.model_validate(raw)
            except ValidationError as exc:
                raise ToolError("некорректный ответ tools/list") from exc
            tools += [
                RemoteTool(
                    name=t.name,
                    description=(t.description or "")[:2000],
                    input_schema=dict(t.input_schema),
                )
                for t in result.tools
            ]
            cursor = result.next_cursor
            if not cursor:
                return tools
        raise ToolError("tools/list: слишком много страниц")

    async def call_tool(
        self, server: McpServerSpec, token: str, name: str, arguments: dict[str, Any]
    ) -> ToolCallResult:
        raw = await self._rpc(
            server, token, "tools/call", {"name": name, "arguments": arguments}, name=name
        )
        try:
            result = CallToolResult.model_validate(raw)
        except ValidationError as exc:
            raise ToolError("некорректный ответ tools/call") from exc
        parts = [c.text for c in result.content if isinstance(c, TextContent)]
        if result.structured_content is not None and not parts:
            parts.append(json.dumps(result.structured_content, ensure_ascii=False))
        text = "\n".join(parts)[:_RESULT_LIMIT] or "(пустой результат)"
        return ToolCallResult(text=text, is_error=bool(result.is_error))


def _response_message(content: bytes, ctype: str, status: int, rid: str) -> dict[str, Any]:
    """JSON-RPC ответ на запрос rid из JSON или SSE."""
    try:
        if ctype == "text/event-stream":
            for event in content.decode("utf-8").split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                )
                if not data:
                    continue
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("id") == rid:
                    return msg
            raise ToolError("в потоке SSE нет ответа на запрос")
        msg = json.loads(content)
    except ValueError as exc:
        raise ToolError(f"MCP-ответ не JSON (HTTP {status})") from exc
    if not isinstance(msg, dict):
        raise ToolError("MCP-ответ не объект JSON-RPC")
    return msg
