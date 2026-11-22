"""Контракт LLM-шлюза для фасада: OpenAI-совместимый Chat Completions (ADR-0001, ADR-0006)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class GatewayUnavailable(Exception):  # noqa: N818
    """Шлюз не ответил (сеть, таймаут): клиенту 502 или 504."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass(frozen=True, slots=True)
class GatewayMeta:
    status: int
    request_id: str | None
    cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    meta: GatewayMeta
    body: Any  # JSON ответа или текст, если шлюз вернул не JSON


class GatewayStream(Protocol):
    meta: GatewayMeta

    async def error_body(self) -> Any:  # noqa: ANN401
        """Тело ответа с ошибкой (meta.status != 200)."""

    def events(self) -> AsyncIterator[bytes]:
        """События SSE как есть, каждое с завершающей пустой строкой."""


class ChatGateway(Protocol):
    async def complete(self, payload: dict[str, Any]) -> GatewayResponse: ...

    def stream(self, payload: dict[str, Any]) -> AbstractAsyncContextManager[GatewayStream]: ...

    async def aclose(self) -> None: ...
