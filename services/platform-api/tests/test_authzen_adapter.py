"""Адаптер AuthZEN: формат запроса для Cerbos, разбор ответа, отказ при сбое PDP."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from antec_platform.adapters.authzen import AuthZenPolicyClient
from antec_platform.policy import PolicyUnavailable, Resource, Subject

pytestmark = pytest.mark.anyio

USER = Subject(id="u1", roles=("platform-user",), attrs={"idp": "entra"})
MODELS = [Resource("model", "chat-default"), Resource("model", "chat-fast")]


def _client(handler: httpx.MockTransport, scope: str | None = None) -> AuthZenPolicyClient:
    client = AuthZenPolicyClient("http://cerbos:3592", 3, scope)
    client._client = httpx.AsyncClient(base_url="http://cerbos:3592/", transport=handler)
    return client


async def test_request_shape_and_decisions() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/access/v1/evaluations"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"evaluations": [{"decision": True}, {"decision": False}]})

    client = _client(httpx.MockTransport(handler))
    assert await client.check(USER, "invoke", MODELS) == [True, False]
    assert seen == [
        {
            "subject": {
                "type": "user",
                "id": "u1",
                "properties": {"idp": "entra", "cerbos.roles": ["platform-user"]},
            },
            "action": {"name": "invoke"},
            "evaluations": [
                {"resource": {"type": "model", "id": "chat-default"}},
                {"resource": {"type": "model", "id": "chat-fast"}},
            ],
        }
    ]


async def test_scope_in_every_resource() -> None:
    """Scope инсталляции (ADR-0008): плоское свойство cerbos.scope у каждого ресурса."""
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"evaluations": [{"decision": True}] * 2})

    client = _client(httpx.MockTransport(handler), scope="acme")
    assert await client.check(USER, "invoke", MODELS) == [True, True]
    assert seen[0]["evaluations"] == [
        {
            "resource": {
                "type": "model",
                "id": "chat-default",
                "properties": {"cerbos.scope": "acme"},
            }
        },
        {"resource": {"type": "model", "id": "chat-fast", "properties": {"cerbos.scope": "acme"}}},
    ]


async def test_no_roles_denied_without_pdp_call() -> None:
    """Cerbos подставил бы роль = subject.type: пустые роли до PDP не доходят."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("PDP не должен вызываться")

    client = _client(httpx.MockTransport(handler))
    assert await client.check(Subject(id="u1", roles=()), "invoke", MODELS) == [False, False]
    assert await client.check(USER, "invoke", []) == []


async def test_batches_over_limit() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["evaluations"])
        sizes.append(n)
        return httpx.Response(200, json={"evaluations": [{"decision": True}] * n})

    client = _client(httpx.MockTransport(handler))
    resources = [Resource("model", f"m{i}") for i in range(120)]
    assert await client.check(USER, "invoke", resources) == [True] * 120
    assert sizes == [50, 50, 20]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"evaluations": [{"decision": True}]}),
        httpx.Response(200, json={"evaluations": [{"decision": "yes"}, {"decision": True}]}),
        httpx.Response(200, json=[]),
    ],
    ids=["5xx", "not-json", "wrong-length", "non-bool", "not-object"],
)
async def test_bad_pdp_response_is_unavailable(response: httpx.Response) -> None:
    client = _client(httpx.MockTransport(lambda _r: response))
    with pytest.raises(PolicyUnavailable):
        await client.check(USER, "invoke", MODELS)


async def test_network_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи", request=request)

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(PolicyUnavailable):
        await client.check(USER, "invoke", MODELS)
    with pytest.raises(PolicyUnavailable):
        await client.ping()


CERBOS_URL = os.environ.get("PLATFORM_TEST_CERBOS_URL")


@pytest.mark.cerbos
@pytest.mark.skipif(not CERBOS_URL, reason="PLATFORM_TEST_CERBOS_URL не задан")
async def test_real_cerbos_with_repo_policies() -> None:
    """Настоящий Cerbos с политиками из policies/cerbos: контракт адаптера и политика вместе."""
    client = AuthZenPolicyClient(CERBOS_URL or "", 3)
    try:
        await client.ping()
        unknown = Resource("model", "gpt-unknown")
        assert await client.check(USER, "invoke", [*MODELS, unknown]) == [True, True, False]
        assert await client.check(USER, "delete", MODELS) == [False, False]
        other = Subject(id="u2", roles=("open-webui-user",))
        assert await client.check(other, "invoke", MODELS) == [False, False]
        # Роль = subject.type ("user") не должна давать прав ни в одной политике.
        assert await client.check(Subject(id="u3", roles=("user",)), "invoke", MODELS) == [
            False,
            False,
        ]
        admin = Subject(id="a1", roles=("platform-admin",))
        assert await client.check(admin, "invoke", [unknown]) == [True]
    finally:
        await client.aclose()


@pytest.mark.cerbos
@pytest.mark.skipif(not CERBOS_URL, reason="PLATFORM_TEST_CERBOS_URL не задан")
async def test_real_cerbos_with_instance_scope() -> None:
    """Политики ядра и dev-инсталляции (instances/dev/policies, scope dev) в одном Cerbos."""
    lab = Resource("model", "chat-lab")
    unknown = Resource("model", "gpt-unknown")
    scoped = AuthZenPolicyClient(CERBOS_URL or "", 3, "dev")
    unscoped = AuthZenPolicyClient(CERBOS_URL or "", 3)
    # Scope без политик инсталляции: решают политики ядра (lenientScopeSearch).
    absent = AuthZenPolicyClient(CERBOS_URL or "", 3, "no-such-instance")
    try:
        assert await scoped.check(USER, "invoke", [*MODELS, lab, unknown]) == [
            True,
            True,
            True,
            False,
        ]
        assert await unscoped.check(USER, "invoke", [lab]) == [False]
        assert await absent.check(USER, "invoke", [*MODELS, lab]) == [True, True, False]
        admin = Subject(id="a1", roles=("platform-admin",))
        assert await scoped.check(admin, "invoke", [unknown]) == [True]
    finally:
        for c in (scoped, unscoped, absent):
            await c.aclose()
