"""Брокер инструментов: белый список, права, обмен токена, аудит (ADR-0010, п. 6)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import FakePolicy, FakeStore

from antec_platform.agents import AgentDefinition
from antec_platform.policy import Subject
from antec_platform.tools import (
    McpServerSpec,
    McpToolSpec,
    RemoteTool,
    ToolBroker,
    ToolCallResult,
    ToolConfigError,
    ToolError,
    load_mcp_servers,
    model_tool_name,
)

pytestmark = pytest.mark.anyio

SERVER = McpServerSpec(
    "mcp-demo",
    "http://mcp-demo:8080/mcp",
    "mcp-demo",
    "2026-07-28",
    (McpToolSpec("get_time", True), McpToolSpec("create_note", False)),
)
AGENT = AgentDefinition(
    name="agent-notes",
    description="",
    model="chat-fast",
    instructions="x",
    tools=("mcp-demo.get_time", "mcp-demo.create_note"),
    max_steps=5,
    max_tool_calls=4,
)


@dataclass
class FakeMcp:
    remote: list[RemoteTool] = field(
        default_factory=lambda: [
            RemoteTool("get_time", "время", {"type": "object"}),
            RemoteTool("create_note", "заметка", {"type": "object"}),
            RemoteTool("drop_all", "лишний", {"type": "object"}),
        ]
    )
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def list_tools(self, server: McpServerSpec, token: str) -> list[RemoteTool]:
        if self.fail:
            raise ToolError("down")
        return self.remote

    async def call_tool(
        self, server: McpServerSpec, token: str, name: str, arguments: dict[str, Any]
    ) -> ToolCallResult:
        self.calls.append((token, name, arguments))
        if self.fail:
            raise ToolError("down")
        return ToolCallResult("готово")

    async def aclose(self) -> None:
        return None


@dataclass
class FakeExchange:
    seen: list[tuple[str, str]] = field(default_factory=list)

    async def exchange(self, subject_token: str, audience: str) -> str:
        self.seen.append((subject_token, audience))
        return f"{audience}-token"

    async def aclose(self) -> None:
        return None


def _broker(mcp: FakeMcp, policy: FakePolicy, store: FakeStore) -> ToolBroker:
    return ToolBroker(
        servers={"mcp-demo": SERVER},
        client=mcp,
        exchanger=FakeExchange(),
        policy=policy,
        audit=store,
        subject=Subject(id="u1", roles=("platform-user",)),
        subject_token="user-token",
        user_id=uuid.uuid7(),
        client_id="open-webui",
        request_id=uuid.uuid7(),
        agent="agent-notes",
    )


def _policy(*allowed: str) -> FakePolicy:
    policy = FakePolicy()
    policy.allowed = {("platform-user", ref) for ref in allowed}
    return policy


async def test_definitions_are_manifest_and_server_intersection() -> None:
    broker = _broker(FakeMcp(), _policy(), FakeStore())
    defs = await broker.definitions(AGENT)
    assert [(d.ref, d.read_only, d.model_name) for d in defs] == [
        ("mcp-demo.get_time", True, "mcp-demo__get_time"),
        ("mcp-demo.create_note", False, "mcp-demo__create_note"),
    ]


async def test_server_down_means_no_tools() -> None:
    broker = _broker(FakeMcp(fail=True), _policy(), FakeStore())
    assert await broker.definitions(AGENT) == []


async def test_call_checks_policy_exchanges_token_and_audits() -> None:
    mcp, store = FakeMcp(), FakeStore()
    policy = FakePolicy()
    policy.allowed = set()
    broker = _broker(mcp, policy, store)
    # FakePolicy разрешает только action invoke; call проверяется отдельно.
    policy_calls: list[str] = []

    async def check(subject: Subject, action: str, resources: list[Any]) -> list[bool]:
        policy_calls.append(action)
        return [r.id == "mcp-demo.create_note" for r in resources]

    policy.check = check  # type: ignore[method-assign]
    result = await broker.call("mcp-demo.create_note", False, {"text": "x"}, "user-1")
    assert result.text == "готово"
    assert mcp.calls == [("mcp-demo-token", "create_note", {"text": "x"})]
    assert policy_calls == ["call"]
    ((ref, outcome, details),) = store.tool_calls
    assert (ref, outcome, details["approved_by"]) == ("mcp-demo.create_note", "allow", "user-1")
    assert "text" not in json.dumps(details)

    denied = await broker.call("mcp-demo.get_time", True, {}, None)
    assert denied.is_error
    assert store.tool_calls[-1][1] == "deny"
    assert len(mcp.calls) == 1


async def test_call_error_is_audited_as_error() -> None:
    mcp, store = FakeMcp(fail=True), FakeStore()
    policy = FakePolicy()

    async def allow(subject: Subject, action: str, resources: list[Any]) -> list[bool]:
        return [True] * len(resources)

    policy.check = allow  # type: ignore[method-assign]
    result = await _broker(mcp, policy, store).call("mcp-demo.get_time", True, {}, None)
    assert result.is_error
    assert store.tool_calls[-1][1] == "error"


def test_load_mcp_servers(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "mcp-demo",
                    "url": "http://mcp-demo:8080/mcp",
                    "audience": "mcp-demo",
                    "protocol_version": "2026-07-28",
                    "tools": [
                        {"name": "get_time", "read_only": True},
                        {"name": "create_note", "read_only": False},
                    ],
                }
            ]
        )
    )
    servers = load_mcp_servers(path, {AGENT.name: AGENT})
    assert servers["mcp-demo"].tool("create_note") == McpToolSpec("create_note", False)
    with pytest.raises(ToolConfigError, match="нет в mcp_servers"):
        load_mcp_servers(None, {AGENT.name: AGENT})


def test_model_tool_name() -> None:
    assert model_tool_name("mcp.demo", "a b") == "mcp_demo__a_b"
    assert len(model_tool_name("s" * 60, "t" * 60)) == 64
