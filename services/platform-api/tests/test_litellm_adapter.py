"""Адаптер LiteLLM: разбор SSE по чанкам, заголовки стоимости."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from antec_platform.adapters.litellm import LiteLLMGateway

pytestmark = pytest.mark.anyio


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _gateway(handler: httpx.MockTransport) -> LiteLLMGateway:
    gw = LiteLLMGateway("http://litellm:4000/v1", "sk-test", 10)
    gw._client = httpx.AsyncClient(  # подмена транспорта для теста
        base_url="http://litellm:4000/v1/",
        transport=handler,
        headers=gw._client.headers,
    )
    return gw


async def test_crlf_split_across_chunks() -> None:
    chunks = [b'data: {"a": 1}\r', b"\n\r\n", b"data: [DONE]\r\n", b"\r\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks(chunks), headers={"x-litellm-call-id": "c1"})

    gw = _gateway(httpx.MockTransport(handler))
    async with gw.stream({"model": "m"}) as stream:
        events = [e async for e in stream.events()]
    assert events == [b'data: {"a": 1}\n\n', b"data: [DONE]\n\n"]
    assert stream.meta.request_id == "c1"
    await gw.aclose()


async def test_cost_header_parsed_and_invalid_ignored() -> None:
    costs = iter(["3.4e-06", "nan", "-1"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200, json={"id": "x"}, headers={"x-litellm-response-cost": next(costs)}
        )

    gw = _gateway(httpx.MockTransport(handler))
    results = [(await gw.complete({"model": "m"})).meta.cost_usd for _ in range(3)]
    assert [str(r) if r is not None else None for r in results] == ["0.0000034", None, None]
    await gw.aclose()
