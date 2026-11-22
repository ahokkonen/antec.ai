"""Запрос Chat Completions: проверка, белый список параметров, новые сообщения, поток SSE.

В шлюз уходят только параметры OpenAI из белого списка (ADR-0006, п. 2): LiteLLM принимает и
свои параметры (`api_base`, `api_key`, `metadata`, `mock_response` и др.), через фасад их
передать нельзя. Поле `user` всегда заменяется идентификатором пользователя Platform API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from antec_platform.errors import ApiError

ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
# Ключи сообщения, которые передаются шлюзу и сохраняются в истории.
MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id", "refusal")
# Параметры OpenAI Chat Completions, которые передаются шлюзу как есть.
PASSTHROUGH = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning_effort",
)
MAX_MESSAGES = 1000


@dataclass(slots=True)
class ChatRequest:
    model: str
    stream: bool
    client_wants_usage: bool
    messages: list[dict[str, Any]]
    params: dict[str, Any]

    def upstream_payload(self, user_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": self.messages, **self.params}
        payload["user"] = user_id
        if self.stream:
            payload["stream"] = True
            # Токены потока нужны для учёта расходов, даже если клиент их не просил.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def new_messages(self) -> list[dict[str, Any]]:
        """Сообщения после последнего ответа ассистента: остальное уже сохранено раньше."""
        last = -1
        for i, msg in enumerate(self.messages):
            if msg["role"] == "assistant":
                last = i
        return self.messages[last + 1 :]


def _bad(message: str, param: str | None = None) -> ApiError:
    return ApiError(400, message, code="invalid_request", param=param)


def parse_chat_request(body: object, models: frozenset[str]) -> ChatRequest:
    if not isinstance(body, dict):
        raise _bad("тело запроса должно быть JSON-объектом")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _bad("нужно поле model", "model")
    if model not in models:
        raise ApiError(
            404,
            f"модель {model!r} недоступна; доступны: {', '.join(sorted(models))}",
            code="model_not_found",
            param="model",
        )
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise _bad("stream должен быть true или false", "stream")
    if body.get("n", 1) != 1:
        raise _bad("поддерживается только n = 1", "n")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _bad("нужен непустой список messages", "messages")
    if len(raw_messages) > MAX_MESSAGES:
        raise _bad(f"не больше {MAX_MESSAGES} сообщений", "messages")
    messages: list[dict[str, Any]] = []
    for i, msg in enumerate(raw_messages):
        if not isinstance(msg, dict) or msg.get("role") not in ROLES:
            raise _bad(f"messages[{i}]: неизвестная роль", f"messages[{i}].role")
        if "content" in msg and not isinstance(msg["content"], (str, list, type(None))):
            raise _bad(f"messages[{i}]: content должен быть строкой или списком частей")
        messages.append({k: msg[k] for k in MESSAGE_KEYS if k in msg})
    options = body.get("stream_options")
    wants_usage = isinstance(options, dict) and options.get("include_usage") is True
    params = {k: body[k] for k in PASSTHROUGH if k in body and body[k] is not None}
    return ChatRequest(
        model=model,
        stream=stream,
        client_wants_usage=wants_usage,
        messages=messages,
        params=params,
    )


def usage_tokens(usage: object) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )


def response_id(body: object) -> str | None:
    """Идентификатор ответа (`id`): по нему шлюз учитывает расход (LiteLLM: SpendLogs)."""
    value = body.get("id") if isinstance(body, dict) else None
    return value[:128] if isinstance(value, str) and value else None


def response_message(body: object) -> dict[str, Any] | None:
    """Ответ ассистента из непотокового ответа (choices[0].message)."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return None
    out = {k: msg[k] for k in MESSAGE_KEYS if k in msg and msg[k] is not None}
    out["role"] = "assistant"
    return out


@dataclass(slots=True)
class StreamAccumulator:
    """Разбирает события SSE шлюза: текст ответа, вызовы инструментов, usage.

    События пересылаются клиенту без изменений, кроме служебного события usage (без choices),
    если клиент его не просил: Platform API сам добавил include_usage.
    """

    client_wants_usage: bool
    content: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    done: bool = False

    def feed(self, event: bytes) -> bytes | None:
        """Принимает одно событие SSE (строки до пустой строки); возвращает, что переслать."""
        data_lines = [line[5:].strip() for line in event.split(b"\n") if line.startswith(b"data:")]
        if not data_lines:
            return event
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            self.done = True
            return event
        try:
            chunk = json.loads(data)
        except ValueError:
            return event
        if not isinstance(chunk, dict):
            return event
        if self.response_id is None:
            self.response_id = response_id(chunk)
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            self._delta(choices[0])
        elif self.usage is not None and not self.client_wants_usage:
            return None
        return event

    def _delta(self, choice: object) -> None:
        if not isinstance(choice, dict):
            return
        if isinstance(choice.get("finish_reason"), str):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        if isinstance(delta.get("content"), str):
            self.content.append(delta["content"])
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                continue
            acc = self.tool_calls.setdefault(
                call["index"],
                {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if isinstance(call.get("id"), str):
                acc["id"] = call["id"]
            fn = call.get("function")
            if isinstance(fn, dict):
                if isinstance(fn.get("name"), str):
                    acc["function"]["name"] += fn["name"]
                if isinstance(fn.get("arguments"), str):
                    acc["function"]["arguments"] += fn["arguments"]

    def message(self) -> dict[str, Any] | None:
        if not self.content and not self.tool_calls:
            return None
        msg: dict[str, Any] = {"role": "assistant", "content": "".join(self.content)}
        if self.tool_calls:
            msg["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        return msg
