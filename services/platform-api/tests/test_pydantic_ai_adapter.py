"""Адаптер Pydantic AI против поддельного LiteLLM (OpenAI-совместимый поток SSE, ADR-0010).

Проверяется контракт AgentRuntime: фрагменты, итог, шаги для model_calls, user = id
пользователя, ошибки шлюза, отмена.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from antec_platform.adapters.pydantic_ai import PydanticAIRuntime
from antec_platform.agents import AgentDefinition, RunOutcome, Turn

pytestmark = pytest.mark.anyio

AGENT = AgentDefinition(
    name="agent-assistant",
    description="",
    model="chat-fast",
    instructions="Ты ассистент dev.",
    tools=(),
    max_steps=3,
    max_tool_calls=0,
)


def _sse(*chunks: dict[str, Any]) -> bytes:
    out = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return out + b"data: [DONE]\n\n"


def _chunk(content: str | None = None, finish: str | None = None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    delta = {"content": content} if content is not None else {}
    return {
        "id": "chatcmpl-gw-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "chat-fast",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


@dataclass
class Gateway:
    status: int = 200
    body: bytes = field(
        default_factory=lambda: _sse(
            _chunk("При"),
            _chunk("вет"),
            _chunk(finish="stop"),
            {
                **_chunk(),
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
            },
        )
    )
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[httpx2.Headers] = field(default_factory=list)
    delay: float = 0.0

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx2.Response(
                self.status, json={"error": {"message": "Budget has been exceeded"}}
            )
        return httpx2.Response(
            200, content=self.body, headers={"content-type": "text/event-stream"}
        )


def _runtime(gw: Gateway) -> PydanticAIRuntime:
    return PydanticAIRuntime(
        {AGENT.name: AGENT},
        "http://litellm:4000/v1",
        "sk-platform",
        30,
        transport=httpx2.MockTransport(gw.handler),
    )


async def _collect(
    rt: PydanticAIRuntime, history: list[Turn] | None = None
) -> tuple[str, RunOutcome]:
    run = rt.start(AGENT, "user-1", history or [], "Привет")
    text = "".join([d async for d in run.deltas()])
    return text, await run.outcome()


async def test_stream_and_outcome() -> None:
    gw = Gateway()
    rt = _runtime(gw)
    try:
        text, outcome = await _collect(rt, [Turn("user", "Q1"), Turn("assistant", "A1")])
    finally:
        await rt.aclose()
    assert text == "Привет"
    assert outcome.status == "ok"
    assert outcome.text == "Привет"
    (step,) = outcome.steps
    assert (step.status, step.prompt_tokens, step.completion_tokens) == ("ok", 11, 2)
    assert step.gateway_request_id == "chatcmpl-gw-1"
    (req,) = gw.requests
    assert req["model"] == "chat-fast"
    assert req["user"] == "user-1"
    assert req["stream"] is True
    roles = [m["role"] for m in req["messages"]]
    assert roles[0] == "system"
    assert "Ты ассистент dev." in req["messages"][0]["content"]
    assert roles[1:] == ["user", "assistant", "user"]
    assert gw.headers[0]["authorization"] == "Bearer sk-platform"


async def test_budget_rejected_passes_status() -> None:
    gw = Gateway(status=400)
    rt = _runtime(gw)
    try:
        text, outcome = await _collect(rt)
    finally:
        await rt.aclose()
    assert text == ""
    assert outcome.status == "rejected"
    assert outcome.http_status == 400
    assert outcome.error == "Budget has been exceeded"
    assert [s.status for s in outcome.steps] == ["rejected"]
    # Повторов в SDK нет: ровно один запрос к шлюзу.
    assert len(gw.requests) == 1


@pytest.mark.parametrize(
    ("status", "code"), [(401, "upstream_auth_error"), (500, "upstream_error")]
)
async def test_gateway_errors_are_502(status: int, code: str) -> None:
    rt = _runtime(Gateway(status=status))
    try:
        _, outcome = await _collect(rt)
    finally:
        await rt.aclose()
    assert outcome.http_status == 502
    assert outcome.error_code == code
    assert "Budget" not in (outcome.error or "")


async def test_cancel() -> None:
    gw = Gateway(delay=5)
    rt = _runtime(gw)
    try:
        run = rt.start(AGENT, "user-1", [], "Привет")
        await asyncio.sleep(0.05)
        run.cancel()
        assert [d async for d in run.deltas()] == []
        outcome = await asyncio.wait_for(run.outcome(), 2)
    finally:
        await rt.aclose()
    assert outcome.status == "cancelled"


def test_steps_status_and_times() -> None:
    """Оборванный ответ = cancelled; время шага = от запроса до ответа (ADR-0010, п. 5)."""
    import datetime as dt

    from pydantic_ai.messages import (
        ModelMessage,
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.usage import RequestUsage

    from antec_platform.adapters.pydantic_ai import _steps

    t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)
    t1 = t0 + dt.timedelta(seconds=3)
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="q")], timestamp=t0),
        ModelResponse(
            parts=[TextPart(content="a")],
            usage=RequestUsage(input_tokens=5, output_tokens=1),
            timestamp=t1,
        ),
        ModelRequest(parts=[UserPromptPart(content="q2")], timestamp=t1),
        ModelResponse(parts=[TextPart(content="об")], timestamp=t1, state="interrupted"),
        ModelResponse(parts=[], state="interrupted"),
    ]
    first, second = _steps(messages)
    assert (first.status, first.started_at, first.finished_at) == ("ok", t0, t1)
    assert (first.prompt_tokens, first.completion_tokens) == (5, 1)
    assert second.status == "cancelled"


def test_banner_disabled_by_adapters_package() -> None:
    import os

    assert os.environ.get("PYDANTIC_AI_NO_BANNER") == "1"
