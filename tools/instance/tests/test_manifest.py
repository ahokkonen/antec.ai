from __future__ import annotations

import copy
from typing import Any

import pytest

from antec_instance.manifest import ManifestError, parse_manifest

BASE: dict[str, Any] = {
    "schema_version": 1,
    "instance": {"id": "acme", "name": "Acme Oy"},
    "core": {"version": "0.1.0"},
    "models": [
        {
            "name": "chat-default",
            "litellm": {
                "model": "anthropic/claude-opus-5-5",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        }
    ],
}


def _with(path: str, value: object) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    node: Any = data
    keys = path.split(".")
    for key in keys[:-1]:
        node = node[int(key)] if isinstance(node, list) else node[key]
    last = keys[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return data


def _problems(data: object) -> str:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(data)
    return "\n".join(exc.value.problems)


def test_minimal_manifest() -> None:
    m = parse_manifest(BASE)
    assert m.instance.id == "acme"
    assert m.mcp_servers == []
    assert m.settings.allowed_clients is None


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        ("schema_version", 3, "schema_version"),
        ("instance.id", "Acme", "instance.id"),
        ("instance.id", "a.b", "instance.id"),
        ("models.0.litellm.api_key", "sk-plain-secret", "os.environ"),
        ("models.0.litellm.api_key", "os.environ/DATABASE_URL", "DATABASE_URL"),
        ("models.0.litellm.api_base", "http://user:pw@host/v1", "учётных данных"),
        ("models.0.litellm.api_base", "file:///etc/passwd", "http(s)"),
        ("models.0.litellm.callbacks", ["x"], "callbacks"),
        ("models.0.name", "Chat Default", "models.0.name"),
        ("core.version", "main", "core.version"),
        ("instance.name", "a\nb", "instance.name"),
        ("instance.name", 'Acme "Oy"', "instance.name"),
        ("instance.name", "a\\b", "instance.name"),
    ],
)
def test_rejected(path: str, value: object, needle: str) -> None:
    assert needle in _problems(_with(path, value))


def test_unknown_top_level_field() -> None:
    assert "extra" in _problems({**BASE, "extra": 1})


def test_duplicate_models() -> None:
    data = copy.deepcopy(BASE)
    data["models"].append(copy.deepcopy(data["models"][0]))
    assert "повторяется" in _problems(data)


def test_mcp_server() -> None:
    server = {
        "name": "crm",
        "url": "http://mcp-crm:8080/mcp",
        "audience": "mcp-crm",
        "protocol_version": "2026-07-28",
        "tools": [{"name": "search", "read_only": True}, {"name": "create_deal"}],
        "secrets": ["api_token"],
    }
    m = parse_manifest({**BASE, "mcp_servers": [server]})
    assert [t.read_only for t in m.mcp_servers[0].tools] == [True, False]
    assert "повторяется" in _problems(
        {**BASE, "mcp_servers": [{**server, "tools": [{"name": "a"}, {"name": "a"}]}]}
    )
    assert "protocol_version" in _problems(
        {**BASE, "mcp_servers": [{**server, "protocol_version": "2024-11-05"}]}
    )
    assert "повторяется" in _problems({**BASE, "mcp_servers": [server, {**server, "name": "crm2"}]})


def test_settings() -> None:
    m = parse_manifest(
        {**BASE, "settings": {"allowed_clients": ["open-webui"], "log_level": "INFO"}}
    )
    assert m.settings.allowed_clients == ["open-webui"]
    assert "allowed_clients" in _problems({**BASE, "settings": {"allowed_clients": []}})
    assert "oidc_issuer" in _problems({**BASE, "settings": {"oidc_issuer": "http://evil"}})
    assert "max_request_bytes" in _problems({**BASE, "settings": {"max_request_bytes": 10}})


def test_int_timeout_accepted() -> None:
    m = parse_manifest(_with("models.0.litellm.timeout", 30))
    assert m.models[0].litellm.timeout == 30


AGENT: dict[str, Any] = {
    "name": "agent-sales",
    "model": "chat-default",
    "instructions": "Ты помощник.",
}


def test_agents_v2() -> None:
    m = parse_manifest({**BASE, "schema_version": 2, "agents": [AGENT]})
    assert m.agents[0].max_steps == 10
    assert m.agents[0].tools == []


def test_agents_need_v2() -> None:
    assert "schema_version: 2" in _problems({**BASE, "agents": [AGENT]})


@pytest.mark.parametrize(
    ("agent", "needle"),
    [
        ({**AGENT, "name": "sales"}, "agents.0.name"),
        ({**AGENT, "model": "chat-x"}, "chat-x"),
        ({**AGENT, "tools": ["crm.search"]}, "crm.search"),
        ({"name": "agent-a", "model": "chat-default"}, "ровно одно"),
        ({**AGENT, "instructions_file": "a.md"}, "ровно одно"),
        ({"name": "agent-a", "model": "chat-default", "instructions_file": "a/../../x.md"}, ".."),
        ({**AGENT, "max_steps": 0}, "max_steps"),
        ({**AGENT, "instructions": "x" * (32 * 1024 + 1)}, "байт"),
    ],
)
def test_agent_rejected(agent: dict[str, Any], needle: str) -> None:
    assert needle in _problems({**BASE, "schema_version": 2, "agents": [agent]})


def test_agent_tool_refs_and_unique() -> None:
    server = {
        "name": "crm",
        "url": "http://mcp-crm:8080/mcp",
        "audience": "mcp-crm",
        "protocol_version": "2026-07-28",
        "tools": [{"name": "search"}],
    }
    data = {**BASE, "schema_version": 2, "mcp_servers": [server]}
    m = parse_manifest({**data, "agents": [{**AGENT, "tools": ["crm.search"]}]})
    assert m.agents[0].tools == ["crm.search"]
    assert "повторяется" in _problems({**data, "agents": [AGENT, AGENT]})


def test_model_name_agent_prefix_reserved() -> None:
    assert "agent-" in _problems(_with("models.0.name", "agent-x"))
