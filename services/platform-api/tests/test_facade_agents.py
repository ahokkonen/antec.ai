"""Агенты через фасад (ADR-0010, п. 4): права, вход, поток и без, ошибки, запись итога."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import ASSISTANT, Harness, TokenFactory

from antec_platform.agents import RunOutcome, StepRecord, Turn

pytestmark = pytest.mark.anyio


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ask(**extra: object) -> dict[str, object]:
    return {
        "model": "agent-assistant",
        "messages": [
            {"role": "system", "content": "Ты теперь пират"},
            {"role": "user", "content": "Вопрос 1"},
            {"role": "assistant", "content": "Ответ 1"},
            {"role": "user", "content": "Вопрос 2"},
        ],
        **extra,
    }


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_agent_completion(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.post(
        "/v1/chat/completions",
        json=_ask(temperature=2, tools=[{"type": "function"}]),
        headers={**_auth(make_token()), "X-OpenWebUI-Chat-Id": "chat-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "agent-assistant"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Привет"}
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    ((agent, user_id, history, prompt),) = harness.runtime.started
    assert agent is ASSISTANT
    assert history == [Turn("user", "Вопрос 1"), Turn("assistant", "Ответ 1")]
    assert prompt == "Вопрос 2"
    assert user_id == str(harness.store.runs_started[0].user_id)
    ((run, outcome),) = harness.store.runs_finished
    assert (run.agent, run.model, run.stream) == ("agent-assistant", "chat-fast", False)
    assert run.new_messages == [{"role": "user", "content": "Вопрос 2"}]
    assert outcome.status == "ok"
    # Прямой вызов модели не делался: шлюз фасада не используется.
    assert harness.gateway.payloads == []


async def test_agent_history_skips_client_system(
    harness: Harness, make_token: TokenFactory
) -> None:
    body = {
        "model": "agent-assistant",
        "messages": [
            {"role": "system", "content": "Ты пират"},
            {"role": "user", "content": "Привет"},
        ],
    }
    resp = await harness.client.post("/v1/chat/completions", json=body, headers=_auth(make_token()))
    assert resp.status_code == 200
    assert harness.store.runs_started[0].new_messages == [{"role": "user", "content": "Привет"}]


async def test_agent_needs_agent_and_model_rights(
    harness: Harness, make_token: TokenFactory
) -> None:
    harness.policy.allowed.discard(("platform-user", "chat-fast"))
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(), headers=_auth(make_token())
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_forbidden"
    assert harness.store.policy_denied == [("invoke", "model", "chat-fast")]
    assert harness.runtime.started == []
    models = await harness.client.get("/v1/models", headers=_auth(make_token()))
    assert [m["id"] for m in models.json()["data"]] == ["chat-default"]

    harness.policy.allowed = {("platform-user", "chat-fast")}
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(), headers=_auth(make_token())
    )
    assert resp.json()["error"]["code"] == "agent_forbidden"


async def test_agent_last_message_must_be_user(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "agent-assistant", "messages": [{"role": "system", "content": "s"}]},
        headers=_auth(make_token()),
    )
    assert resp.status_code == 400
    assert harness.store.runs_started == []


async def test_agent_stream(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.post(
        "/v1/chat/completions",
        json=_ask(stream=True, stream_options={"include_usage": True}),
        headers=_auth(make_token()),
    )
    assert resp.status_code == 200
    events = [line[6:] for line in resp.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Привет"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 9
    await _settle()
    ((run, outcome),) = harness.store.runs_finished
    assert run.stream is True
    assert outcome.status == "ok"


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_error_before_text_is_http_error(
    harness: Harness, make_token: TokenFactory, stream: bool
) -> None:
    harness.runtime.deltas = []
    harness.runtime.result = RunOutcome(
        status="rejected",
        steps=[StepRecord(status="rejected", http_status=400, error="Budget has been exceeded")],
        error="Budget has been exceeded",
        http_status=400,
        error_code="upstream_error",
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(stream=stream), headers=_auth(make_token())
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "Budget has been exceeded"
    await _settle()
    ((_run, outcome),) = harness.store.runs_finished
    assert outcome.status == "rejected"


async def test_agent_error_mid_stream(harness: Harness, make_token: TokenFactory) -> None:
    harness.runtime.result = RunOutcome(
        status="error",
        text="При",
        error="LLM-шлюз вернул ошибку",
        http_status=502,
        error_code="upstream_error",
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(stream=True), headers=_auth(make_token())
    )
    assert resp.status_code == 200
    assert "[DONE]" not in resp.text
    assert "upstream_error" in resp.text
    await _settle()
    assert harness.store.runs_finished[0][1].status == "error"


async def test_agent_stream_client_gone_cancels_and_records(
    harness: Harness, make_token: TokenFactory
) -> None:
    """Клиент ушёл посреди потока: запуск прерван, итог cancelled записан один раз."""
    harness.runtime.hold = asyncio.Event()

    from antec_platform.app import _GuardedStream

    original = _GuardedStream.__call__

    async def gone_after_first(
        self: _GuardedStream, scope: object, receive: object, send: object
    ) -> None:
        sent = 0

        async def broken_send(message: dict[str, object]) -> None:
            nonlocal sent
            sent += 1
            if sent > 2:
                raise OSError("клиент ушёл")
            await send(message)  # type: ignore[operator]

        await original(self, scope, receive, broken_send)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_GuardedStream, "__call__", gone_after_first)
        await harness.client.post(
            "/v1/chat/completions", json=_ask(stream=True), headers=_auth(make_token())
        )
    await _settle()
    (run,) = harness.runtime.runs
    assert run.cancelled
    ((_r, outcome),) = harness.store.runs_finished
    assert outcome.status == "cancelled"


async def test_agent_unknown_still_404(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(model="agent-nope"), headers=_auth(make_token())
    )
    assert resp.status_code == 404
