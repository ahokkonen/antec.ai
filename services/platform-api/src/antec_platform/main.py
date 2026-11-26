"""Сборка приложения из настроек: настоящие хранилище, шлюз и проверка токенов."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from antec_platform.adapters import otel
from antec_platform.adapters.authzen import AuthZenPolicyClient
from antec_platform.adapters.keycloak import KeycloakTokenExchange
from antec_platform.adapters.litellm import LiteLLMGateway
from antec_platform.adapters.mcp import HttpMcpClient
from antec_platform.adapters.pydantic_ai import PydanticAIRuntime, instrument_agents
from antec_platform.agents import AgentConfigError, AgentRuntime, load_agents
from antec_platform.app import Deps, create_app
from antec_platform.auth import JwksCache, TokenVerifier
from antec_platform.data_classes import load_clearances
from antec_platform.db import create_engine
from antec_platform.settings import Settings
from antec_platform.store import PgStore
from antec_platform.tools import McpClient, TokenExchanger, load_mcp_servers


def build_app(settings: Settings) -> FastAPI:
    # agents.json проверяется при сборке: ошибка конфигурации агентов не даёт запустить сервис.
    agents = load_agents(settings.agents_file, settings.models)
    servers = load_mcp_servers(settings.mcp_servers_file, agents)
    clearances = load_clearances(settings.models_file, settings.models)
    uses_tools = any(a.tools for a in agents.values())
    if uses_tools and (settings.oidc_token_url is None or settings.oidc_client_secret is None):
        raise AgentConfigError(
            "инструментам агентов нужны PLATFORM_OIDC_TOKEN_URL и секрет "
            "platform_oidc_client_secret (обмен токена, ADR-0010, п. 6)"
        )
    engine = create_engine(settings)
    http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    # Телеметрия до создания агентов и клиентов (ADR-0013); без адреса Collector выключена.
    telemetry: otel.Telemetry | None = None
    if settings.otel_endpoint:
        telemetry = otel.setup(settings.otel_endpoint, "antec-platform-api")
        instrument_agents()

    async def factory() -> Deps:
        jwks = JwksCache(settings.oidc_jwks_url, http, ttl=settings.jwks_cache_seconds)
        runtime: AgentRuntime | None = None
        if agents:
            runtime = PydanticAIRuntime(
                agents,
                settings.gateway_url,
                settings.gateway_api_key.get_secret_value(),
                settings.gateway_timeout_seconds,
            )
        mcp_client: McpClient | None = None
        exchanger: TokenExchanger | None = None
        if uses_tools and settings.oidc_token_url and settings.oidc_client_secret:
            mcp_client = HttpMcpClient(settings.mcp_timeout_seconds)
            exchanger = KeycloakTokenExchange(
                settings.oidc_token_url,
                settings.oidc_client_id,
                settings.oidc_client_secret.get_secret_value(),
                settings.mcp_timeout_seconds,
            )
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
            policy=AuthZenPolicyClient(
                settings.policy_url, settings.policy_timeout_seconds, settings.policy_scope
            ),
            models=settings.models,
            max_request_bytes=settings.max_request_bytes,
            policy_scope=settings.policy_scope,
            agents=agents,
            runtime=runtime,
            mcp_servers=servers,
            mcp_client=mcp_client,
            token_exchange=exchanger,
            approval_ttl_seconds=settings.approval_ttl_seconds,
            clearances=clearances,
            default_data_class=settings.default_data_class,
        )

    async def shutdown(deps: Deps) -> None:
        if deps.runtime is not None:
            await deps.runtime.aclose()
        if deps.mcp_client is not None:
            await deps.mcp_client.aclose()
        if deps.token_exchange is not None:
            await deps.token_exchange.aclose()
        await deps.gateway.aclose()
        await deps.policy.aclose()
        await http.aclose()
        await engine.dispose()
        if telemetry is not None:
            telemetry.shutdown()

    return create_app(factory, shutdown)
