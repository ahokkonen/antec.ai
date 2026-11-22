"""Обмен токена в Keycloak (RFC 8693): форма запроса, кэш, ошибки."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from antec_platform.adapters.keycloak import KeycloakTokenExchange
from antec_platform.tools import ToolError

pytestmark = pytest.mark.anyio

URL = "http://keycloak:8080/realms/antec/protocol/openid-connect/token"


async def test_exchange_and_cache() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "mcp-token", "expires_in": 300})

    ex = KeycloakTokenExchange(URL, "platform-api", "s3cret", 5, httpx.MockTransport(handler))
    try:
        assert await ex.exchange("user-token", "mcp-demo") == "mcp-token"
        assert await ex.exchange("user-token", "mcp-demo") == "mcp-token"
        await ex.exchange("user-token", "mcp-other")
    finally:
        await ex.aclose()
    assert len(seen) == 2
    form = {k: v[0] for k, v in parse_qs(seen[0].content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": "user-token",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "mcp-demo",
        "scope": "mcp-demo",
    }
    basic = base64.b64decode(seen[0].headers["authorization"].split()[1]).decode()
    assert basic == "platform-api:s3cret"


async def test_exchange_rejected() -> None:
    ex = KeycloakTokenExchange(
        URL, "platform-api", "s", 5, httpx.MockTransport(lambda _r: httpx.Response(400))
    )
    try:
        with pytest.raises(ToolError, match="отклонён"):
            await ex.exchange("t", "mcp-demo")
    finally:
        await ex.aclose()


async def test_short_lived_token_not_cached() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": f"t{calls}", "expires_in": 10})

    ex = KeycloakTokenExchange(URL, "platform-api", "s", 5, httpx.MockTransport(handler))
    try:
        assert await ex.exchange("u", "a") == "t1"
        assert await ex.exchange("u", "a") == "t2"
    finally:
        await ex.aclose()
