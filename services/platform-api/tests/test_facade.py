"""Фасад целиком: HTTP-запросы к приложению с подставными хранилищем и шлюзом."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from conftest import FakeStream, Harness, TokenFactory, ok_meta

from antec_platform.adapters.gateway import GatewayMeta, GatewayResponse, GatewayUnavailable

pytestmark = pytest.mark.anyio

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ответ"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _chat(**extra: object) -> dict[str, object]:
    return {"model": "chat-default", "messages": [{"role": "user", "content": "Привет"}], **extra}


def _sse(obj: object) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


async def _settle(h: Harness) -> None:
    await h.deps.drain()
    await asyncio.sleep(0)


async def test_no_token_is_401_with_challenge(harness: Harness) -> None:
    resp = await harness.client.post("/v1/chat/completions", json=_chat())
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert resp.json()["error"]["type"] == "authentication_error"
    assert resp.headers["x-request-id"]
    assert harness.gateway.payloads == []


async def test_invalid_token_is_401_without_audit(
    harness: Harness, make_token: TokenFactory
) -> None:
    resp = await harness.client.get("/v1/models", headers=_auth(make_token(exp_in=-600)))
    assert resp.status_code == 401
    assert harness.store.denied == []


async def test_foreign_client_is_403_and_audited(
    harness: Harness, make_token: TokenFactory
) -> None:
    resp = await harness.client.get("/v1/models", headers=_auth(make_token(azp="mcp-m365")))
    assert resp.status_code == 403
    assert harness.store.denied == [("sub-1", "client")]


async def test_disabled_user_is_403(harness: Harness, make_token: TokenFactory) -> None:
    harness.store.disabled.add("sub-1")
    resp = await harness.client.get("/v1/models", headers=_auth(make_token()))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "user_disabled"
    assert harness.store.denied == [("sub-1", "disabled")]


async def test_models(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.get("/v1/models", headers=_auth(make_token()))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == [
        "chat-default",
        "chat-fast",
        "agent-assistant",
    ]


async def test_completion_records_call(harness: Harness, make_token: TokenFactory) -> None:
    harness.gateway.response = GatewayResponse(meta=ok_meta(), body=COMPLETION)
    resp = await harness.client.post(
        "/v1/chat/completions",
        json=_chat(api_base="http://evil", user="spoofed"),
        headers={**_auth(make_token()), "X-OpenWebUI-Chat-Id": "chat-42"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == COMPLETION
    user = next(iter(harness.store.users.values()))
    (payload,) = harness.gateway.payloads
    assert payload["user"] == str(user.id)
    assert "api_base" not in payload
    (call, result) = harness.store.finished[0]
    assert call.id.hex == resp.headers["x-request-id"].replace("-", "")
    assert call.new_messages == [{"role": "user", "content": "Привет"}]
    assert (result.status, result.prompt_tokens, result.completion_tokens) == ("ok", 7, 3)
    assert str(result.cost_usd) == "0.0012"
    assert result.gateway_request_id == "chatcmpl-1"
    assert result.response_message == {"role": "assistant", "content": "Ответ"}
    assert list(harness.store.conversations) == [(user.id, "open-webui", "chat-42")]


async def test_budget_exceeded_passes_through(harness: Harness, make_token: TokenFactory) -> None:
    body = {
        "error": {"message": "Budget has been exceeded", "type": "budget_exceeded", "code": "400"}
    }
    harness.gateway.response = GatewayResponse(
        meta=GatewayMeta(status=400, request_id=None, cost_usd=None), body=body
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "budget_exceeded"
    assert harness.store.finished[0][1].status == "rejected"


async def test_upstream_5xx_is_502(harness: Harness, make_token: TokenFactory) -> None:
    harness.gateway.response = GatewayResponse(
        meta=GatewayMeta(status=500, request_id=None, cost_usd=None),
        body={"error": {"message": "internal: key sk-abc..."}},
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    assert resp.status_code == 502
    assert "sk-" not in resp.text
    assert harness.store.finished[0][1].status == "error"


@pytest.mark.parametrize(("is_timeout", "status"), [(False, 502), (True, 504)])
async def test_gateway_unavailable(
    harness: Harness, make_token: TokenFactory, is_timeout: bool, status: int
) -> None:
    harness.gateway.unavailable = GatewayUnavailable("нет", timeout=is_timeout)
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    assert resp.status_code == status
    assert harness.store.finished[0][1].status == "error"


async def test_request_too_large(harness: Harness, make_token: TokenFactory) -> None:
    big = _chat(messages=[{"role": "user", "content": "x" * 70000}])
    resp = await harness.client.post("/v1/chat/completions", json=big, headers=_auth(make_token()))
    assert resp.status_code == 413
    assert harness.store.started == []


async def test_not_json(harness: Harness, make_token: TokenFactory) -> None:
    resp = await harness.client.post(
        "/v1/chat/completions", content=b"{", headers=_auth(make_token())
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_json"


async def test_stream_forwards_events_and_records(
    harness: Harness, make_token: TokenFactory
) -> None:
    chunks = [
        _sse({"id": "chatcmpl-s", "choices": [{"delta": {"content": "При"}}]}),
        _sse(
            {
                "id": "chatcmpl-s",
                "choices": [{"delta": {"content": "вет"}, "finish_reason": "stop"}],
            }
        ),
        _sse(
            {
                "id": "chatcmpl-s",
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    harness.gateway.stream_value = FakeStream(meta=ok_meta(cost=None), chunks=chunks)
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == chunks[0] + chunks[1] + chunks[3]
    await _settle(harness)
    (_call, result) = harness.store.finished[0]
    assert result.status == "ok"
    assert (result.prompt_tokens, result.completion_tokens) == (4, 2)
    assert result.response_message == {"role": "assistant", "content": "Привет"}
    assert result.cost_usd is None
    assert result.gateway_request_id == "chatcmpl-s"
    assert harness.gateway.stream_closed == 1
    assert harness.gateway.payloads[0]["stream_options"] == {"include_usage": True}


async def test_stream_interrupted(harness: Harness, make_token: TokenFactory) -> None:
    chunks = [_sse({"choices": [{"delta": {"content": "x"}}]}), b"data: [DONE]\n\n"]
    harness.gateway.stream_value = FakeStream(meta=ok_meta(), chunks=chunks, raise_after=1)
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
    )
    assert resp.status_code == 200
    assert b"upstream_interrupted" in resp.content
    await _settle(harness)
    assert harness.store.finished[0][1].status == "error"


async def test_stream_without_done_is_error(harness: Harness, make_token: TokenFactory) -> None:
    harness.gateway.stream_value = FakeStream(
        meta=ok_meta(), chunks=[_sse({"choices": [{"delta": {"content": "x"}}]})]
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
    )
    assert b"upstream_incomplete" in resp.content
    await _settle(harness)
    assert harness.store.finished[0][1].status == "error"


async def test_stream_upstream_error_before_body(
    harness: Harness, make_token: TokenFactory
) -> None:
    harness.gateway.stream_value = FakeStream(
        meta=GatewayMeta(status=429, request_id="r", cost_usd=None),
        chunks=[],
        error={"error": {"message": "rate limited", "type": "rate_limit_error"}},
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"
    assert harness.store.finished[0][1].status == "rejected"
    assert harness.gateway.stream_closed == 1


async def test_health(harness: Harness) -> None:
    assert (await harness.client.get("/healthz")).status_code == 200
    ready = await harness.client.get("/readyz")
    assert ready.status_code == 200, ready.text


async def test_unknown_route_is_openai_error(harness: Harness) -> None:
    resp = await harness.client.get("/v1/embeddings")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("upstream_status", [401, 403])
async def test_upstream_auth_error_is_502(
    harness: Harness, make_token: TokenFactory, upstream_status: int
) -> None:
    harness.gateway.response = GatewayResponse(
        meta=GatewayMeta(status=upstream_status, request_id=None, cost_usd=None),
        body={
            "error": {"message": "Authentication Error, invalid key sk-...", "type": "auth_error"}
        },
    )
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_auth_error"
    assert "sk-" not in resp.text
    assert harness.store.finished[0][1].status == "error"


async def test_stream_finalized_when_body_never_read(
    harness: Harness, make_token: TokenFactory
) -> None:
    """Клиент ушёл до первой итерации тела: поток шлюза закрыт, итог вызова записан."""
    harness.gateway.stream_value = FakeStream(meta=ok_meta(), chunks=[b"data: [DONE]\n\n"])

    from antec_platform.app import _GuardedStream

    captured: list[_GuardedStream] = []
    original = _GuardedStream.__call__

    async def fail_before_body(
        self: _GuardedStream, scope: object, receive: object, send: object
    ) -> None:
        captured.append(self)

        async def broken_send(_message: object) -> None:
            raise OSError("клиент ушёл")

        await original(self, scope, receive, broken_send)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_GuardedStream, "__call__", fail_before_body)
        await harness.client.post(
            "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
        )
    await _settle(harness)
    assert captured
    assert harness.gateway.stream_closed == 1
    assert harness.store.finished[0][1].status == "cancelled"


async def test_stream_unexpected_error_is_recorded_as_error(
    harness: Harness, make_token: TokenFactory
) -> None:
    class Boom(FakeStream):
        async def events(self) -> AsyncIterator[bytes]:
            yield _sse({"choices": [{"delta": {"content": "x"}}]})
            raise ValueError("неожиданно")

    harness.gateway.stream_value = Boom(meta=ok_meta(), chunks=[])
    await harness.client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(make_token())
    )
    await _settle(harness)
    assert harness.store.finished[0][1].status == "error"


async def test_model_forbidden_by_policy(harness: Harness, make_token: TokenFactory) -> None:
    harness.policy.allowed = {("platform-user", "chat-fast")}
    resp = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_forbidden"
    assert harness.store.policy_denied == [("invoke", "model", "chat-default")]
    assert harness.gateway.payloads == []
    assert harness.store.started == []


async def test_no_platform_roles_means_no_access(
    harness: Harness, make_token: TokenFactory
) -> None:
    token = make_token(platform_roles=[])
    resp = await harness.client.get("/v1/models", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["data"] == []
    resp = await harness.client.post("/v1/chat/completions", json=_chat(), headers=_auth(token))
    assert resp.status_code == 403


async def test_models_filtered_by_policy(harness: Harness, make_token: TokenFactory) -> None:
    harness.policy.allowed = {("platform-user", "chat-fast")}
    resp = await harness.client.get("/v1/models", headers=_auth(make_token()))
    assert [m["id"] for m in resp.json()["data"]] == ["chat-fast"]
    ((subject, action, resources),) = harness.policy.calls
    assert action == "invoke"
    assert subject.roles == ("platform-user",)
    assert subject.attrs == {"client_id": "open-webui", "idp": "entra"}
    assert [r.id for r in resources] == ["chat-default", "chat-fast", "agent-assistant"]
    assert [r.kind for r in resources] == ["model", "model", "agent"]


async def test_policy_down_fails_closed(harness: Harness, make_token: TokenFactory) -> None:
    harness.policy.down = True
    harness.gateway.response = GatewayResponse(meta=ok_meta(), body=COMPLETION)
    chat = await harness.client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(make_token())
    )
    models = await harness.client.get("/v1/models", headers=_auth(make_token()))
    ready = await harness.client.get("/readyz")
    assert (chat.status_code, models.status_code, ready.status_code) == (503, 503, 503)
    assert chat.json()["error"]["code"] == "policy_unavailable"
    assert harness.gateway.payloads == []
