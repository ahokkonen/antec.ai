"""MCP 2026-07-28, Streamable HTTP без сессий: server/discover, tools/list, tools/call.

Только для стенда и CI: заметки в памяти процесса. Токен проверяется полностью, как у
настоящего MCP-сервера: инструменты работают с правами пользователя из токена (ADR-0007).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

PROTOCOL_VERSION = "2026-07-28"
_MIN_REFRESH = 30.0
_MAX_AGE = 600.0
SERVER_INFO = {"name": "mcp-demo", "version": "0.1.0"}
log = logging.getLogger("mcp_demo")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_time",
        "description": "Текущее время UTC в формате ISO 8601.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_note",
        "description": "Сохранить заметку пользователя.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Текст заметки"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


@dataclass(slots=True)
class Config:
    issuer: str
    jwks_url: str
    audience: str = "mcp-demo"
    authorized_party: str = "platform-api"


class Unauthorized(Exception):  # noqa: N818
    pass


@dataclass(slots=True)
class Verifier:
    config: Config
    http: httpx.AsyncClient
    _keys: dict[str, Any] = field(default_factory=dict)
    _loaded: float = -1e9

    async def _refresh(self) -> None:
        resp = await self.http.get(self.config.jwks_url, timeout=5)
        resp.raise_for_status()
        self._keys = {
            k["kid"]: jwt.PyJWK(k).key for k in resp.json()["keys"] if k.get("use", "sig") == "sig"
        }
        self._loaded = time.monotonic()

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise Unauthorized("токен не разобран") from exc
        # Обновление JWKS: по сроку или на неизвестный kid, но не чаще раза в 30 с: поток
        # токенов со случайным kid не превращается в поток запросов к Keycloak.
        age = time.monotonic() - self._loaded
        if (kid not in self._keys and age > _MIN_REFRESH) or age > _MAX_AGE:
            try:
                await self._refresh()
            except httpx.HTTPError as exc:
                raise Unauthorized("JWKS недоступен") from exc
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise Unauthorized("неизвестный kid")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized(f"токен отклонён: {type(exc).__name__}") from exc
        if claims.get("azp") != self.config.authorized_party:
            raise Unauthorized("токен выдан не Platform API")
        return claims


def _error(status: int, rid: object, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _result(rid: object, result: dict[str, Any]) -> JSONResponse:
    meta = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "result": {"_meta": meta, "resultType": "complete", **result}}
    )


def _text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def create_app(config: Config, http: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="mcp-demo", docs_url=None, redoc_url=None, openapi_url=None)
    verifier = Verifier(config, http or httpx.AsyncClient(trust_env=False))
    notes: list[dict[str, str]] = []
    app.state.notes = notes

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        if request.headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
            return _error(400, None, -32602, f"нужен MCP-Protocol-Version: {PROTOCOL_VERSION}")
        accept = request.headers.get("accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return _error(406, None, -32600, "Accept: application/json, text/event-stream")
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = await verifier.verify(auth[7:])
        except Unauthorized as exc:
            log.info("отказ: %s", exc)
            return Response(
                status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
            )
        try:
            body = json.loads(await request.body())
        except ValueError:
            return _error(400, None, -32700, "тело не JSON")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _error(400, None, -32600, "ожидается JSON-RPC 2.0")
        method, rid = body.get("method"), body.get("id")
        if "id" not in body:
            return Response(status_code=202)
        if request.headers.get("mcp-method") != method:
            return _error(400, rid, -32001, "Mcp-Method не совпадает с method")
        raw_params = body.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw_meta = params.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if (
            meta.get("io.modelcontextprotocol/protocolVersion", PROTOCOL_VERSION)
            != PROTOCOL_VERSION
        ):
            return _error(400, rid, -32001, "версия в _meta не совпадает с заголовком")
        if method == "server/discover":
            return _result(
                rid,
                {
                    "supportedVersions": [PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                    "cacheScope": "public",
                    "ttlMs": 60000,
                },
            )
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS, "cacheScope": "public", "ttlMs": 60000})
        if method == "tools/call":
            if request.headers.get("mcp-name") != params.get("name"):
                return _error(400, rid, -32001, "Mcp-Name не совпадает с params.name")
            return _result(rid, _call(params, claims, notes))
        return _error(404, rid, -32601, f"метод {method!r} не поддерживается")

    return app


def _call(
    params: dict[str, Any], claims: dict[str, Any], notes: list[dict[str, str]]
) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "get_time":
        return _text(dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    if name == "create_note":
        text = args.get("text") if isinstance(args, dict) else None
        if not isinstance(text, str) or not text.strip():
            return _text("нужен непустой text", is_error=True)
        note = {"id": uuid.uuid4().hex[:8], "owner": str(claims["sub"]), "text": text[:1000]}
        notes.append(note)
        log.info("note created owner=%s id=%s", note["owner"], note["id"])
        return _text(f"Заметка {note['id']} сохранена")
    return _text(f"неизвестный инструмент {name!r}", is_error=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config(
        issuer=os.environ["MCP_DEMO_ISSUER"],
        jwks_url=os.environ["MCP_DEMO_JWKS_URL"],
        audience=os.environ.get("MCP_DEMO_AUDIENCE", "mcp-demo"),
        authorized_party=os.environ.get("MCP_DEMO_AUTHORIZED_PARTY", "platform-api"),
    )
    uvicorn.run(create_app(config), host="0.0.0.0", port=8080, log_level="info")  # noqa: S104
