"""Адаптер LiteLLM: OpenAI-совместимый HTTP API, без SDK (ADR-0001, ADR-0006, п. 1).

Особенности LiteLLM, известные только здесь: стоимость в заголовке `x-litellm-response-cost`
(только без потока), идентификатор вызова в `x-litellm-call-id`. Расход в учёте LiteLLM
(SpendLogs) записан по `id` ответа, поэтому фасад хранит `id` ответа, а заголовок как запасной.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from antec_platform.adapters.gateway import GatewayMeta, GatewayResponse, GatewayUnavailable

_MAX_EVENT_BYTES = 1024 * 1024


def _meta(resp: httpx.Response) -> GatewayMeta:
    cost: Decimal | None = None
    raw = resp.headers.get("x-litellm-response-cost")
    if raw:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            value = None
        if value is not None and value.is_finite() and value >= 0:
            cost = value
    request_id = resp.headers.get("x-litellm-call-id")
    return GatewayMeta(
        status=resp.status_code,
        request_id=request_id[:128] if request_id else None,
        cost_usd=cost,
    )


def _json_or_text(resp: httpx.Response) -> Any:  # noqa: ANN401
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


class _Stream:
    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp
        self.meta = _meta(resp)

    async def error_body(self) -> Any:  # noqa: ANN401
        await self._resp.aread()
        return _json_or_text(self._resp)

    async def events(self) -> AsyncIterator[bytes]:
        buf = b""
        async for chunk in self._resp.aiter_bytes():
            buf += chunk.replace(b"\r\n", b"\n")
            while (sep := buf.find(b"\n\n")) != -1:
                event, buf = buf[: sep + 2], buf[sep + 2 :]
                yield event
            if len(buf) > _MAX_EVENT_BYTES:
                raise GatewayUnavailable("слишком длинное событие SSE от шлюза")
        if buf.strip():
            yield buf + b"\n\n"


class LiteLLMGateway:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=5.0),
            # Без прокси из окружения и без переадресаций: адрес шлюза задан явно.
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, payload: dict[str, Any]) -> GatewayResponse:
        try:
            resp = await self._client.post("chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise GatewayUnavailable("шлюз не ответил вовремя", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"шлюз недоступен: {type(exc).__name__}") from exc
        return GatewayResponse(meta=_meta(resp), body=_json_or_text(resp))

    @asynccontextmanager
    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[_Stream]:
        request = self._client.build_request("POST", "chat/completions", json=payload)
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise GatewayUnavailable("шлюз не ответил вовремя", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"шлюз недоступен: {type(exc).__name__}") from exc
        try:
            yield _Stream(resp)
        finally:
            await resp.aclose()
