"""Классы данных (ADR-0011): допуски моделей, ввод пользователя, инструменты, Cerbos."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from conftest import ASSISTANT, Harness, TokenFactory
from test_tools import AGENT, SERVER, FakeExchange, FakeMcp

from antec_platform.data_classes import DataClassConfigError, allows, load_clearances
from antec_platform.policy import Resource, Subject
from antec_platform.tools import DENIED_TEXT, McpServerSpec, McpToolSpec, ToolBroker

pytestmark = pytest.mark.anyio


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_allows_order() -> None:
    assert allows("restricted", "confidential")
    assert allows("internal", "internal")
    assert not allows("internal", "confidential")
    with pytest.raises(DataClassConfigError):
        allows("secret", "public")


def test_load_clearances(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps([{"name": "chat-lab", "max_data_class": "internal"}]))
    assert load_clearances(path, ["chat-lab", "chat-fast"]) == {
        "chat-lab": "internal",
        "chat-fast": "restricted",
    }
    assert load_clearances(None, ["a"]) == {"a": "restricted"}
    path.write_text(json.dumps([{"name": "chat-lab", "max_data_class": "secret"}]))
    with pytest.raises(DataClassConfigError):
        load_clearances(path, ["chat-lab"])


async def test_model_below_input_class_hidden_and_refused(
    harness: Harness, make_token: TokenFactory
) -> None:
    harness.deps.default_data_class = "confidential"
    harness.deps.clearances = {"chat-fast": "internal"}
    models = await harness.client.get("/v1/models", headers=_auth(make_token()))
    ids = [m["id"] for m in models.json()["data"]]
    # chat-fast (internal) скрыт, и агент на нём тоже.
    assert ids == ["chat-default"]
    resp = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "chat-fast", "messages": [{"role": "user", "content": "x"}]},
        headers=_auth(make_token()),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "data_class_forbidden"
    assert harness.store.deny_reasons == ["data_class"]
    agent = await harness.client.post(
        "/v1/chat/completions",
        json={"model": ASSISTANT.name, "messages": [{"role": "user", "content": "x"}]},
        headers=_auth(make_token()),
    )
    assert agent.json()["error"]["code"] == "data_class_forbidden"
    assert harness.runtime.started == []


async def test_data_class_attribute_reaches_policy(
    harness: Harness, make_token: TokenFactory
) -> None:
    await harness.client.get("/v1/models", headers=_auth(make_token()))
    ((_subject, _action, resources),) = harness.policy.calls
    assert {r.attrs["data_class"] for r in resources} == {"internal"}


async def test_tool_above_model_clearance_denied_before_call() -> None:
    from conftest import FakePolicy, FakeStore

    store, mcp = FakeStore(), FakeMcp()
    policy = FakePolicy()
    seen: list[Resource] = []

    async def allow(subject: Subject, action: str, resources: list[Resource]) -> list[bool]:
        seen.extend(resources)
        return [True] * len(resources)

    policy.check = allow  # type: ignore[method-assign]
    server = McpServerSpec(
        SERVER.name,
        SERVER.url,
        SERVER.audience,
        SERVER.protocol_version,
        (
            McpToolSpec("get_time", True, "public"),
            McpToolSpec("create_note", False, "confidential"),
        ),
    )
    broker = ToolBroker(
        servers={server.name: server},
        client=mcp,
        exchanger=FakeExchange(),
        policy=policy,
        audit=store,
        subject=Subject(id="u1", roles=("platform-user",)),
        subject_token="t",
        user_id=uuid.uuid7(),
        client_id="open-webui",
        request_id=uuid.uuid7(),
        agent=AGENT.name,
        clearance="internal",
        default_class="internal",
    )
    assert not await broker.authorize("mcp-demo.create_note", False)
    result = await broker.call("mcp-demo.create_note", False, {"text": "x"}, "u1")
    assert (result.text, result.is_error) == (DENIED_TEXT, True)
    assert mcp.calls == []
    assert all(d["reason"] == "data_class" for _ref, o, d in store.tool_calls if o == "deny")
    assert store.tool_calls[0][2]["model_clearance"] == "internal"
    # Инструмент в пределах допуска: политика получает его класс.
    ok = await broker.call("mcp-demo.get_time", True, {}, None)
    assert not ok.is_error
    assert seen[-1].attrs == {"data_class": "public"}
