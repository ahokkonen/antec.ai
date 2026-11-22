"""Подтверждение инструментов через фасад (ADR-0010, п. 7)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from dataclasses import replace

import pytest
from conftest import ASSISTANT, Harness, TokenFactory

from antec_platform.agents import PendingCall, RunOutcome, StepRecord

pytestmark = pytest.mark.anyio

CHAT = {"X-OpenWebUI-Chat-Id": "chat-approvals"}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **CHAT}


def _ask(text: str, **extra: object) -> dict[str, object]:
    return {"model": "agent-assistant", "messages": [{"role": "user", "content": text}], **extra}


def _waiting() -> RunOutcome:
    return RunOutcome(
        status="awaiting_approval",
        text="Сохраню заметку.",
        steps=[StepRecord(status="ok", http_status=200, prompt_tokens=5, completion_tokens=2)],
        pending=[PendingCall("c1", "mcp-demo.create_note", {"text": "хлеб"})],
        state={"messages": []},
    )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _pause(harness: Harness, token: str, stream: bool = False) -> str:
    harness.runtime.result = _waiting()
    harness.runtime.deltas = ["Сохраню заметку."]
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask("Сохрани хлеб", stream=stream), headers=_auth(token)
    )
    assert resp.status_code == 200
    if stream:
        events = [line[6:] for line in resp.text.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        content = "".join(
            json.loads(e)["choices"][0]["delta"].get("content", "")
            for e in events[:-1]
            if json.loads(e)["choices"]
        )
    else:
        content = resp.json()["choices"][0]["message"]["content"]
    await _settle()
    match = re.search(r"подтверждаю ([A-Z0-9]{4})", content)
    assert match, content
    assert "mcp-demo.create_note" in content
    assert '"хлеб"' in content
    return match.group(1)


@pytest.mark.parametrize("stream", [False, True])
async def test_pause_records_code_and_expiry(
    harness: Harness, make_token: TokenFactory, stream: bool
) -> None:
    code = await _pause(harness, make_token(), stream)
    approval = harness.store.approvals[-1]
    assert approval is not None
    assert approval.code == code
    assert approval.expires_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=14)
    ((_run, outcome),) = harness.store.runs_finished
    assert outcome.status == "awaiting_approval"
    assert code in outcome.text


async def test_approve_resumes_with_decision(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    code = await _pause(harness, token)
    harness.runtime.result = RunOutcome(status="ok", text="Готово", steps=[])
    harness.runtime.deltas = ["Готово"]
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(f"Подтверждаю {code.lower()}"), headers=_auth(token)
    )
    assert resp.json()["choices"][0]["message"]["content"] == "Готово"
    resume = harness.runtime.resumes[-1]
    assert resume is not None
    assert resume.approved == {"c1": True}
    assert resume.decided_by == str(harness.store.runs_started[0].user_id)
    assert harness.store.resolved[-1][1] == "approved"
    first, second = harness.store.runs_started
    assert second.parent_run_id == first.id


async def test_cancel_resumes_with_denial(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    await _pause(harness, token)
    harness.runtime.result = RunOutcome(status="ok", text="Не сохраняю", steps=[])
    await harness.client.post("/v1/chat/completions", json=_ask("Отмена"), headers=_auth(token))
    resume = harness.runtime.resumes[-1]
    assert resume is not None
    assert resume.approved == {"c1": False}
    assert harness.store.resolved[-1][1] == "denied"


async def test_wrong_code_cancels_without_model(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    await _pause(harness, token)
    started = len(harness.runtime.started)
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask("подтверждаю ZZZZ"), headers=_auth(token)
    )
    assert "не совпадает" in resp.json()["choices"][0]["message"]["content"]
    assert len(harness.runtime.started) == started
    assert harness.store.resolved[-1][1] == "denied"


async def test_other_message_supersedes(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    await _pause(harness, token)
    harness.runtime.result = RunOutcome(status="ok", text="Сейчас 12:00", steps=[])
    await harness.client.post(
        "/v1/chat/completions", json=_ask("Который час?"), headers=_auth(token)
    )
    assert harness.store.resolved[-1][1] == "denied"
    assert harness.runtime.resumes[-1] is None
    assert harness.runtime.started[-1][3] == "Который час?"


async def test_expired(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    code = await _pause(harness, token)
    ((run_id, (owner, pending)),) = harness.store.pending.items()
    past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    harness.store.pending[run_id] = (owner, replace(pending, expires_at=past))
    started = len(harness.runtime.started)
    resp = await harness.client.post(
        "/v1/chat/completions", json=_ask(f"подтверждаю {code}"), headers=_auth(token)
    )
    assert "истёк" in resp.json()["choices"][0]["message"]["content"]
    assert len(harness.runtime.started) == started
    assert harness.store.resolved[-1][1] == "expired"


async def test_without_chat_id_points_to_decision_endpoint(
    harness: Harness, make_token: TokenFactory
) -> None:
    harness.runtime.result = _waiting()
    resp = await harness.client.post(
        "/v1/chat/completions",
        json=_ask("Сохрани"),
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    content = resp.json()["choices"][0]["message"]["content"]
    assert "/v1/agent-runs/" in content
    assert "подтверждаю" not in content


async def test_decision_endpoint(harness: Harness, make_token: TokenFactory) -> None:
    token = make_token()
    await _pause(harness, token)
    run_id = harness.store.runs_started[0].id
    harness.runtime.result = RunOutcome(status="ok", text="Готово", steps=[])
    bad = await harness.client.post(
        f"/v1/agent-runs/{run_id}/decision", json={"decision": "maybe"}, headers=_auth(token)
    )
    assert bad.status_code == 400
    other = await harness.client.post(
        f"/v1/agent-runs/{run_id}/decision",
        json={"decision": "approve"},
        headers=_auth(make_token(sub="someone-else")),
    )
    assert other.status_code == 404
    resp = await harness.client.post(
        f"/v1/agent-runs/{run_id}/decision", json={"decision": "approve"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Готово"
    assert harness.runtime.resumes[-1] is not None
    again = await harness.client.post(
        f"/v1/agent-runs/{run_id}/decision", json={"decision": "approve"}, headers=_auth(token)
    )
    assert again.status_code == 404


async def test_pending_of_other_agent_is_superseded(
    harness: Harness, make_token: TokenFactory
) -> None:
    token = make_token()
    await _pause(harness, token)
    other = replace(ASSISTANT, name="agent-other")
    harness.deps.agents[other.name] = other
    harness.policy.allowed.add(("platform-user", other.name))
    harness.runtime.result = RunOutcome(status="ok", text="ok", steps=[])
    await harness.client.post(
        "/v1/chat/completions",
        json={**_ask("подтверждаю ABCD"), "model": "agent-other"},
        headers=_auth(token),
    )
    assert harness.store.resolved[-1][1] == "denied"
    assert harness.runtime.resumes[-1] is None
