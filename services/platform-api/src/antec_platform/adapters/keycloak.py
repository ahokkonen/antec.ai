"""TokenExchanger: обмен токена пользователя на токен MCP-сервера в Keycloak (RFC 8693).

Standard token exchange V2 (ADR-0007, п. 3): клиент `platform-api` со своим секретом меняет
access token пользователя (aud platform-api) на токен с `audience` = `scope` = аудитория
MCP-сервера; срок не больше исходного. Кэш в памяти по (хэш токена, аудитория) до истечения
минус запас; токены в журнал не пишутся.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

from antec_platform.tools import ToolError

log = logging.getLogger(__name__)

_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS = "urn:ietf:params:oauth:token-type:access_token"
_MARGIN = 30.0
_CACHE_LIMIT = 1000


class KeycloakTokenExchange:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = token_url
        self._client_id = client_id
        self._secret = client_secret
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def exchange(self, subject_token: str, audience: str) -> str:
        key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            resp = await self._http.post(
                self._url,
                data={
                    "grant_type": _GRANT,
                    "subject_token": subject_token,
                    "subject_token_type": _ACCESS,
                    "requested_token_type": _ACCESS,
                    "audience": audience,
                    "scope": audience,
                },
                auth=(self._client_id, self._secret),
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"обмен токена недоступен: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            log.warning("обмен токена для %s: HTTP %s", audience, resp.status_code)
            raise ToolError(f"обмен токена для {audience} отклонён (HTTP {resp.status_code})")
        try:
            payload = resp.json()
            token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in", 60))
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolError("некорректный ответ обмена токена") from exc
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
        self._cache[key] = (token, now + max(0.0, expires_in - _MARGIN))
        return token
