"""Сборка приложения из настроек: настоящие хранилище, шлюз и проверка токенов."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from antec_platform.adapters.authzen import AuthZenPolicyClient
from antec_platform.adapters.litellm import LiteLLMGateway
from antec_platform.app import Deps, create_app
from antec_platform.auth import JwksCache, TokenVerifier
from antec_platform.db import create_engine
from antec_platform.settings import Settings
from antec_platform.store import PgStore


def build_app(settings: Settings) -> FastAPI:
    engine = create_engine(settings)
    http = httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def factory() -> Deps:
        jwks = JwksCache(settings.oidc_jwks_url, http, ttl=settings.jwks_cache_seconds)
        return Deps(
            store=PgStore(engine),
            gateway=LiteLLMGateway(
                settings.gateway_url,
                settings.gateway_api_key.get_secret_value(),
                settings.gateway_timeout_seconds,
            ),
            verifier=TokenVerifier(
                jwks=jwks,
                issuer=settings.oidc_issuer,
                audience=settings.oidc_audience,
                allowed_clients=settings.allowed_clients,
                leeway=settings.jwt_leeway_seconds,
            ),
            jwks=jwks,
            policy=AuthZenPolicyClient(settings.policy_url, settings.policy_timeout_seconds),
            models=settings.models,
            max_request_bytes=settings.max_request_bytes,
        )

    async def shutdown(deps: Deps) -> None:
        await deps.gateway.aclose()
        await deps.policy.aclose()
        await http.aclose()
        await engine.dispose()

    return create_app(factory, shutdown)
