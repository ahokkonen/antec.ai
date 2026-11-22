"""MCP-клиент 2026-07-28 против поддельного сервера: заголовки, _meta, JSON и SSE, ошибки."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from antec_platform.adapters.mcp import HttpMcpClient
from antec_platform.tools import McpServerSpec, McpToolSpec, ToolError

pytestmark = pytest.mark.anyio

SERVER = McpServerSpec(
    name="mcp-demo",
    url="http://mcp-demo:8080/mcp",
    audience="mcp-demo",
    protocol_version="2026-07-28",
    tools=(McpToolSpec("get_time", True),),
)


@dataclass
class Server:
    responses: list[httpx.Response] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        resp = self.responses.pop(0)
        body = json.loads(request.content)
        if resp.headers.get("content-type") == "application/json":
            payload = json.loads(resp.content)
            payload["id"] = body["id"]
            return httpx.Response(resp.status_code, json=payload)
        text = resp.text.replace("__ID__", body["id"])
        return httpx.Response(
            resp.status_code, text=text, headers={"content-type": "text/event-stream"}
        )


def _result(result: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"jsonrpc": "2.0", "id": "x", "result": result})


def _client(server: Server) -> HttpMcpClient:
    return HttpMcpClient(5, transport=httpx.MockTransport(server.handler))


async def test_list_tools_headers_and_meta() -> None:
    server = Server(
        [
            _result(
                {
                    "tools": [
                        {"name": "get_time", "description": "t", "inputSchema": {"type": "object"}}
                    ],
                    "resultType": "complete",
                    "cacheScope": "public",
                    "ttlMs": 1000,
                }
            )
        ]
    )
    client = _client(server)
    try:
        tools = await client.list_tools(SERVER, "tok-1")
    finally:
        await client.aclose()
    assert [(t.name, t.description) for t in tools] == [("get_time", "t")]
    (req,) = server.requests
    assert req.headers["authorization"] == "Bearer tok-1"
    assert req.headers["mcp-protocol-version"] == "2026-07-28"
    assert req.headers["mcp-method"] == "tools/list"
    assert "text/event-stream" in req.headers["accept"]
    meta = json.loads(req.content)["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {}


async def test_call_tool_sse_and_is_error() -> None:
    sse = (
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":"__ID__","result":{"content":[{"type":"text","text":"ok"}],'
        '"isError":true,"resultType":"complete"}}\n\n'
    )
    server = Server([httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})])
    client = _client(server)
    try:
        result = await client.call_tool(SERVER, "tok", "get_time", {"a": 1})
    finally:
        await client.aclose()
    assert (result.text, result.is_error) == ("ok", True)
    (req,) = server.requests
    assert req.headers["mcp-name"] == "get_time"
    assert json.loads(req.content)["params"]["arguments"] == {"a": 1}


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx.Response(401), "отклонил токен"),
        (
            httpx.Response(
                404, json={"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "no"}}
            ),
            "-32601",
        ),
        (_result({"resultType": "input_required", "inputRequests": {}}), "ввода"),
        (
            httpx.Response(200, text="not json", headers={"content-type": "application/json"}),
            "JSON",
        ),
    ],
)
async def test_errors(response: httpx.Response, needle: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if response.headers.get(
            "content-type"
        ) == "application/json" and response.content.startswith(b"{"):
            payload = json.loads(response.content)
            payload["id"] = json.loads(request.content)["id"]
            return httpx.Response(response.status_code, json=payload)
        return response

    client = HttpMcpClient(5, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ToolError, match=needle):
            await client.call_tool(SERVER, "tok", "get_time", {})
    finally:
        await client.aclose()


async def test_old_protocol_refused() -> None:
    client = HttpMcpClient(5, transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    old = McpServerSpec("s", "http://s/mcp", "s", "2025-11-25", ())
    try:
        with pytest.raises(ToolError, match="2025-11-25"):
            await client.list_tools(old, "tok")
    finally:
        await client.aclose()
