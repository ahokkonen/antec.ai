from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from mcp_demo.app import Config, create_app

pytestmark = pytest.mark.anyio

ISSUER = "http://localhost:8080/realms/antec"
JWKS = "http://keycloak:8080/certs"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {
    **jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True),
    "kid": "k1",
    "use": "sig",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _token(**over: Any) -> str:  # noqa: ANN401
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "mcp-demo",
        "azp": "platform-api",
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        **over,
    }
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


JWKS_CALLS: list[int] = []


def _jwks(_request: httpx.Request) -> httpx.Response:
    JWKS_CALLS.append(1)
    return httpx.Response(200, json={"keys": [JWK]})


@pytest.fixture
def app() -> FastAPI:
    JWKS_CALLS.clear()
    jwks = httpx.AsyncClient(transport=httpx.MockTransport(_jwks))
    return create_app(Config(issuer=ISSUER, jwks_url=JWKS), jwks)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp-demo") as c:
        yield c


async def _post(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    h = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
        "Authorization": f"Bearer {token or _token()}",
    }
    if method == "tools/call" and params:
        h["Mcp-Name"] = params["name"]
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return await client.post("/mcp", json=body, headers=h)


async def test_list_and_call(client: httpx.AsyncClient, app: FastAPI) -> None:
    tools = (await _post(client, "tools/list")).json()["result"]
    assert [t["name"] for t in tools["tools"]] == ["get_time", "create_note"]
    assert tools["resultType"] == "complete"
    r = (await _post(client, "tools/call", {"name": "get_time", "arguments": {}})).json()
    assert r["result"]["isError"] is False
    assert r["result"]["content"][0]["text"].endswith("+00:00")
    note = {"name": "create_note", "arguments": {"text": "x"}}
    r = (await _post(client, "tools/call", note)).json()
    assert "сохранена" in r["result"]["content"][0]["text"]
    assert app.state.notes[0]["owner"] == "user-1"
    bad = (await _post(client, "tools/call", {"name": "create_note", "arguments": {}})).json()
    assert bad["result"]["isError"] is True


@pytest.mark.parametrize(
    "token",
    [
        _token(aud="other"),
        _token(azp="open-webui"),
        _token(iss="http://evil"),
        _token(exp=int(time.time()) - 3600),
        "not-a-jwt",
    ],
)
async def test_bad_token_401(client: httpx.AsyncClient, token: str) -> None:
    assert (await _post(client, "tools/list", token=token)).status_code == 401


async def test_protocol_checks(client: httpx.AsyncClient) -> None:
    old = {"MCP-Protocol-Version": "2025-11-25"}
    assert (await _post(client, "tools/list", headers=old)).status_code == 400
    wrong = {"Mcp-Method": "tools/call"}
    assert (await _post(client, "tools/list", headers=wrong)).status_code == 400
    assert (await _post(client, "nope")).status_code == 404
    name = {"Mcp-Name": "x"}
    assert (
        await _post(client, "tools/call", {"name": "get_time"}, headers=name)
    ).status_code == 400
    discover = (await _post(client, "server/discover")).json()["result"]
    assert discover["supportedVersions"] == ["2026-07-28"]


async def test_unknown_kid_does_not_hammer_jwks(client: httpx.AsyncClient) -> None:
    assert (await _post(client, "tools/list")).status_code == 200
    for i in range(5):
        forged = jwt.encode({"sub": "x"}, KEY, algorithm="RS256", headers={"kid": f"k{i + 10}"})
        assert (await _post(client, "tools/list", token=forged)).status_code == 401
    assert len(JWKS_CALLS) == 1
