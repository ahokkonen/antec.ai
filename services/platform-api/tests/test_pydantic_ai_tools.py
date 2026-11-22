"""Инструменты и подтверждения в адаптере Pydantic AI (ADR-0010, пп. 6-7).

Поддельный LiteLLM отдаёт по очереди ответы с вызовом инструмента и текстом; ToolSession
поддельная. Проверяется: read-only вызов сразу, необратимый = awaiting_approval с ожидающим
вызовом и состоянием, продолжение с одобрением вызывает инструмент от имени решившего,
продолжение с отказом не вызывает.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from antec_platform.adapters.pydantic_ai import PydanticAIRuntime
from antec_platform.agents import AgentDefinition, Resume, RunOutcome
from antec_platform.tools import ToolCallResult, ToolDefinition

pytestmark = pytest.mark.anyio

AGENT = AgentDefinition(
    name="agent-notes",
    description="",
    model="chat-fast",
    instructions="Ты помощник заметок.",
    tools=("mcp-demo.get_time", "mcp-demo.create_note"),
    max_steps=6,
    max_tool_calls=4,
)
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "additionalProperties": False,
}


def _sse(*chunks: dict[str, Any]) -> bytes:
    return (
        b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
    )


def _base(choices: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "chat-fast",
        "choices": choices,
        **extra,
    }


def tool_call(name: str, args: dict[str, Any], call_id: str) -> bytes:
    return _sse(
        _base(
            [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        ),
        _base([{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
        _base([], usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}),
    )


def text(content: str) -> bytes:
    return _sse(
        _base([{"index": 0, "delta": {"content": content}, "finish_reason": None}]),
        _base([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        _base([], usage={"prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33}),
    )


@dataclass
class Gateway:
    bodies: list[bytes]
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return httpx2.Response(
            200, content=self.bodies.pop(0), headers={"content-type": "text/event-stream"}
        )


@dataclass
class Session:
    allowed: bool = True
    calls: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)
    authorized: list[str] = field(default_factory=list)

    async def definitions(self, agent: AgentDefinition) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                "mcp-demo.get_time",
                "mcp-demo",
                "get_time",
                "mcp-demo__get_time",
                "время",
                {"type": "object", "properties": {}},
                True,
            ),
            ToolDefinition(
                "mcp-demo.create_note",
                "mcp-demo",
                "create_note",
                "mcp-demo__create_note",
                "заметка",
                SCHEMA,
                False,
            ),
        ]

    async def authorize(self, ref: str, read_only: bool) -> bool:
        self.authorized.append(ref)
        return self.allowed

    async def call(
        self, ref: str, read_only: bool, arguments: dict[str, Any], approved_by: str | None
    ) -> ToolCallResult:
        self.calls.append((ref, arguments, approved_by))
        return ToolCallResult("12:00 UTC" if ref.endswith("get_time") else "Заметка abc сохранена")


async def _run(
    gw: Gateway, session: Session, prompt: str | None = "q", resume: Resume | None = None
) -> RunOutcome:
    rt = PydanticAIRuntime(
        {AGENT.name: AGENT},
        "http://litellm:4000/v1",
        "sk",
        30,
        transport=httpx2.MockTransport(gw.handler),
    )
    try:
        run = rt.start(AGENT, "user-1", [], prompt, session, resume)
        [d async for d in run.deltas()]
        return await run.outcome()
    finally:
        await rt.aclose()


async def test_read_only_tool_runs_without_approval() -> None:
    gw = Gateway([tool_call("mcp-demo__get_time", {}, "c1"), text("Сейчас 12:00")])
    session = Session()
    outcome = await _run(gw, session)
    assert outcome.status == "ok"
    assert outcome.text == "Сейчас 12:00"
    assert session.calls == [("mcp-demo.get_time", {}, None)]
    assert len(outcome.steps) == 2
    tools = {t["function"]["name"] for t in gw.requests[0]["tools"]}
    assert tools == {"mcp-demo__get_time", "mcp-demo__create_note"}


async def test_irreversible_tool_waits_then_runs_after_approval() -> None:
    gw = Gateway([tool_call("mcp-demo__create_note", {"text": "купить хлеб"}, "c2")])
    session = Session()
    waiting = await _run(gw, session)
    assert waiting.status == "awaiting_approval"
    assert session.calls == []
    assert session.authorized == ["mcp-demo.create_note"]
    (pending,) = waiting.pending
    assert (pending.call_id, pending.ref, pending.arguments) == (
        "c2",
        "mcp-demo.create_note",
        {"text": "купить хлеб"},
    )
    assert waiting.state is not None
    json.dumps(waiting.state)  # состояние сериализуется в JSONB

    gw2 = Gateway([text("Заметка сохранена")])
    resumed = await _run(gw2, session, None, Resume(waiting.state, {"c2": True}, "user-1"))
    assert resumed.status == "ok"
    assert session.calls == [("mcp-demo.create_note", {"text": "купить хлеб"}, "user-1")]
    # Модель получила результат инструмента в истории продолжения.
    tool_msgs = [m for m in gw2.requests[0]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "Заметка abc сохранена"


async def test_denied_tool_is_not_called() -> None:
    gw = Gateway([tool_call("mcp-demo__create_note", {"text": "x"}, "c3")])
    session = Session()
    waiting = await _run(gw, session)
    assert waiting.state is not None
    gw2 = Gateway([text("Хорошо, не сохраняю")])
    resumed = await _run(gw2, session, None, Resume(waiting.state, {"c3": False}, "user-1"))
    assert resumed.status == "ok"
    assert session.calls == []
    tool_msgs = [m for m in gw2.requests[0]["messages"] if m["role"] == "tool"]
    assert "отклонил" in tool_msgs[0]["content"]


async def test_forbidden_tool_is_not_offered_for_approval() -> None:
    gw = Gateway([tool_call("mcp-demo__create_note", {"text": "x"}, "c4"), text("Нет прав")])
    session = Session(allowed=False)
    outcome = await _run(gw, session)
    assert outcome.status == "ok"
    assert outcome.pending == []
    assert session.calls == []
    tool_msgs = [m for m in gw.requests[1]["messages"] if m["role"] == "tool"]
    assert "запрещён" in tool_msgs[0]["content"]
