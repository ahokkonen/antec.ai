"""Разбор запроса Chat Completions и событий SSE."""

from __future__ import annotations

import json

import pytest

from antec_platform.chat import (
    StreamAccumulator,
    parse_chat_request,
    response_message,
    usage_tokens,
)
from antec_platform.errors import ApiError

MODELS = frozenset({"chat-default", "chat-fast"})


def _req(**body: object) -> dict[str, object]:
    return {"model": "chat-default", "messages": [{"role": "user", "content": "привет"}], **body}


def test_whitelist_drops_gateway_specific_params_and_overrides_user() -> None:
    chat = parse_chat_request(
        _req(
            temperature=0.2,
            api_base="http://evil",
            api_key="sk-x",
            metadata={"tags": ["x"]},
            mock_response="hi",
            user="someone-else",
            extra_body={"a": 1},
        ),
        MODELS,
    )
    payload = chat.upstream_payload("user-uuid")
    assert payload == {
        "model": "chat-default",
        "messages": [{"role": "user", "content": "привет"}],
        "temperature": 0.2,
        "user": "user-uuid",
    }


def test_message_keys_whitelisted() -> None:
    chat = parse_chat_request(
        _req(messages=[{"role": "user", "content": "x", "api_key": "sk", "name": "u"}]), MODELS
    )
    assert chat.messages == [{"role": "user", "content": "x", "name": "u"}]


def test_stream_requests_usage() -> None:
    chat = parse_chat_request(_req(stream=True), MODELS)
    assert chat.client_wants_usage is False
    assert chat.upstream_payload("u")["stream_options"] == {"include_usage": True}
    chat = parse_chat_request(_req(stream=True, stream_options={"include_usage": True}), MODELS)
    assert chat.client_wants_usage is True


def test_unknown_model_is_404() -> None:
    with pytest.raises(ApiError) as info:
        parse_chat_request(_req(model="gpt-5"), MODELS)
    assert info.value.status == 404
    assert info.value.code == "model_not_found"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="not-object"),
        pytest.param({"messages": [{"role": "user", "content": "x"}]}, id="no-model"),
        pytest.param(_req(messages=[]), id="empty-messages"),
        pytest.param(_req(messages=[{"role": "root", "content": "x"}]), id="bad-role"),
        pytest.param(_req(messages=[{"role": "user", "content": 5}]), id="bad-content"),
        pytest.param(_req(stream="yes"), id="bad-stream"),
        pytest.param(_req(n=2), id="n-2"),
    ],
)
def test_bad_requests(body: object) -> None:
    with pytest.raises(ApiError) as info:
        parse_chat_request(body, MODELS)
    assert info.value.status == 400


def test_new_messages_after_last_assistant() -> None:
    chat = parse_chat_request(
        _req(
            messages=[
                {"role": "system", "content": "s"},
                {"role": "user", "content": "1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "2"},
            ]
        ),
        MODELS,
    )
    assert chat.new_messages() == [{"role": "user", "content": "2"}]
    first = parse_chat_request(
        _req(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "1"}]),
        MODELS,
    )
    assert [m["role"] for m in first.new_messages()] == ["system", "user"]


def test_response_message_and_usage() -> None:
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok", "extra": 1}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    assert response_message(body) == {"role": "assistant", "content": "ok"}
    assert usage_tokens(body["usage"]) == (3, 2)
    assert usage_tokens({"prompt_tokens": "3"}) == (None, None)
    assert response_message({"choices": []}) is None


def _event(obj: object) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def test_stream_accumulator_strips_unrequested_usage() -> None:
    acc = StreamAccumulator(client_wants_usage=False)
    events = [
        _event({"choices": [{"delta": {"role": "assistant", "content": "При"}}]}),
        _event({"choices": [{"delta": {"content": "вет"}, "finish_reason": "stop"}]}),
        _event({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
        b"data: [DONE]\n\n",
    ]
    forwarded = [acc.feed(e) for e in events]
    assert forwarded == [events[0], events[1], None, events[3]]
    assert acc.done
    assert acc.message() == {"role": "assistant", "content": "Привет"}
    assert usage_tokens(acc.usage) == (5, 2)
    assert acc.finish_reason == "stop"


def test_stream_accumulator_keeps_requested_usage_and_comments() -> None:
    acc = StreamAccumulator(client_wants_usage=True)
    usage = _event({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    assert acc.feed(usage) == usage
    assert acc.feed(b": ping\n\n") == b": ping\n\n"
    assert acc.feed(b"data: not-json\n\n") == b"data: not-json\n\n"


def test_stream_accumulator_tool_calls() -> None:
    acc = StreamAccumulator(client_wants_usage=False)
    acc.feed(
        _event(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "get", "arguments": "{"},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    acc.feed(
        _event(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]}
        )
    )
    assert acc.message() == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get", "arguments": "{}"}}
        ],
    }


def test_stream_usage_in_regular_chunk_is_forwarded() -> None:
    """LiteLLM кладёт usage в последний чанк с непустым choices: он пересылается как есть."""
    acc = StreamAccumulator(client_wants_usage=False)
    chunk = _event(
        {
            "id": "chatcmpl-x",
            "choices": [{"index": 0, "delta": {}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 4},
        }
    )
    assert acc.feed(chunk) == chunk
    assert usage_tokens(acc.usage) == (14, 4)
    assert acc.response_id == "chatcmpl-x"
