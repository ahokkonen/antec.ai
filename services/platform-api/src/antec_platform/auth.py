"""Проверка access token Keycloak (ADR-0006, п. 3; ADR-0007).

Принимаются только токены RS256 с ключом из JWKS realm, нужным issuer, сроком и типом Bearer.
Аудитория и клиент (azp) проверяются после подписи отдельно: токен с чужой аудиторией подписан
нашим IdP, значит пользователь известен, и отказ записывается в аудит (DeniedToken). Невалидные
токены (подпись, срок, формат) дают InvalidToken и попадают только в лог.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256"]
# Повторная загрузка JWKS при неизвестном kid не чаще, чем раз в это число секунд:
# иначе поток токенов с выдуманным kid превращается в поток запросов к Keycloak.
_REFRESH_MIN_INTERVAL = 30.0


class InvalidToken(Exception):  # noqa: N818 - имя отражает смысл, а не тип ошибки
    """Токен не прошёл проверку подписи, срока или формата."""


class DeniedToken(Exception):  # noqa: N818
    """Токен валиден, но выдан не для Platform API или не разрешённому клиенту."""

    def __init__(self, reason: str, principal: Principal) -> None:
        super().__init__(reason)
        self.reason = reason
        self.principal = principal


class JwksUnavailable(Exception):  # noqa: N818
    """JWKS не удалось получить: проверить токен нельзя (503, а не 401)."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Пользователь, доказанный подписью IdP."""

    issuer: str
    subject: str
    client_id: str
    idp: str | None
    entra_tid: str | None
    entra_oid: str | None
    name: str | None
    email: str | None
    # Роли клиента platform-api в Keycloak (claim platform_roles, ADR-0009).
    roles: tuple[str, ...] = ()


ROLES_CLAIM = "platform_roles"
_ROLE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")


def _roles(claims: dict[str, Any]) -> tuple[str, ...]:
    value = claims.get(ROLES_CLAIM)
    if not isinstance(value, list):
        return ()
    roles = {r for r in value if isinstance(r, str) and _ROLE_RE.fullmatch(r)}
    if len(roles) < len(set(map(str, value))):
        # Роль с недопустимым именем не даёт прав; без этой записи отказ не объяснить.
        log.warning("в %s отброшены роли с недопустимым именем", ROLES_CLAIM)
    return tuple(sorted(roles))


def _opt_str(claims: dict[str, Any], key: str, limit: int = 256) -> str | None:
    value = claims.get(key)
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


class JwksCache:
    """Ключи JWKS с кэшем на `ttl` секунд и перезагрузкой при неизвестном kid."""

    def __init__(self, url: str, client: httpx.AsyncClient, ttl: float) -> None:
        self._url = url
        self._client = client
        self._ttl = ttl
        self._keys: dict[str, jwt.PyJWK] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return bool(self._keys)

    async def _load(self) -> None:
        try:
            resp = await self._client.get(self._url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JwksUnavailable(f"JWKS недоступен: {type(exc).__name__}") from exc
        keys: dict[str, jwt.PyJWK] = {}
        for item in data.get("keys", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or item.get("use", "sig") != "sig":
                continue
            kid = item.get("kid")
            if not isinstance(kid, str) or item.get("kty") != "RSA":
                continue
            try:
                keys[kid] = jwt.PyJWK(item, algorithm="RS256")
            except jwt.PyJWKError:
                log.warning("пропущен некорректный ключ JWKS kid=%s", kid)
        if not keys:
            raise JwksUnavailable("в JWKS нет ключей подписи RSA")
        self._keys = keys
        self._loaded_at = time.monotonic()

    async def refresh(self) -> None:
        async with self._lock:
            await self._load()

    async def _reload(self) -> None:
        """Перезагрузка под блокировкой. Пока в кэше есть ключи, сбой Keycloak их не стирает:
        токены, подписанные известным ключом, продолжают проверяться (Keycloak перезапускается),
        следующая попытка не раньше чем через _REFRESH_MIN_INTERVAL."""
        try:
            await self._load()
        except JwksUnavailable:
            if not self._keys:
                raise
            log.warning("JWKS недоступен, используются ключи из кэша")
            self._loaded_at = time.monotonic() - self._ttl + _REFRESH_MIN_INTERVAL

    async def get(self, kid: str) -> jwt.PyJWK | None:
        if not self._keys or time.monotonic() - self._loaded_at > self._ttl:
            async with self._lock:
                if not self._keys or time.monotonic() - self._loaded_at > self._ttl:
                    await self._reload()
        key = self._keys.get(kid)
        if key is None and time.monotonic() - self._loaded_at > _REFRESH_MIN_INTERVAL:
            async with self._lock:
                if time.monotonic() - self._loaded_at > _REFRESH_MIN_INTERVAL:
                    await self._reload()
            key = self._keys.get(kid)
        return key


class TokenVerifier:
    def __init__(
        self,
        *,
        jwks: JwksCache,
        issuer: str,
        audience: str,
        allowed_clients: list[str],
        leeway: int,
    ) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience
        self._allowed_clients = frozenset(allowed_clients)
        self._leeway = leeway

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken("заголовок токена не читается") from exc
        if header.get("alg") not in ALGORITHMS:
            raise InvalidToken("неподдерживаемый алгоритм")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise InvalidToken("нет kid")
        key = await self._jwks.get(kid)
        if key is None:
            raise InvalidToken("неизвестный kid")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    # Аудитория проверяется ниже: отказ по ней пишется в аудит.
                    "verify_aud": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc

        if claims.get("typ", "Bearer") != "Bearer":
            raise InvalidToken("не access token")
        subject = _opt_str(claims, "sub")
        if subject is None:
            raise InvalidToken("нет sub")
        principal = Principal(
            issuer=self._issuer,
            subject=subject,
            client_id=_opt_str(claims, "azp", 64) or "",
            idp=_opt_str(claims, "idp", 64),
            entra_tid=_opt_str(claims, "entra_tid", 64),
            entra_oid=_opt_str(claims, "entra_oid", 64),
            name=_opt_str(claims, "name"),
            email=_opt_str(claims, "email"),
            roles=_roles(claims),
        )
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self._audience not in audiences:
            raise DeniedToken("audience", principal)
        if principal.client_id not in self._allowed_clients:
            raise DeniedToken("client", principal)
        return principal


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value or len(value) > 16384:
        return None
    return value
