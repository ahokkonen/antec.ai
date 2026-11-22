"""Настройки Platform API.

Несекретные параметры приходят из окружения (`PLATFORM_*`, задаются в compose), секретные только
из файлов в каталоге секретов, который выгружает secrets-render из OpenBao (ADR-0006, п. 10).
В окружении и argv секретов нет.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL

DEFAULT_SECRETS_DIR = Path("/run/antec-secrets")

_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# Scope политик инсталляции = id инсталляции из манифеста (ADR-0008, п. 4).
_SCOPE_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")


def _split_list(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class DatabaseSettings(BaseSettings):
    """Подключение к PostgreSQL. Пароль: файл `database_password` в каталоге секретов."""

    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_",
        secrets_dir=DEFAULT_SECRETS_DIR,
        extra="ignore",
        frozen=True,
    )

    database_host: str = "postgres"
    database_port: Annotated[int, Field(ge=1, le=65535)] = 5432
    database_name: str = "platform"
    database_user: str = "platform_app"
    database_password: SecretStr
    database_pool_size: Annotated[int, Field(ge=1, le=50)] = 10
    # Роль сервиса, которой миграции выдают права (ADR-0006, п. 9).
    app_role: str = "platform_app"

    @field_validator("app_role", "database_user", "database_name")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("ожидается идентификатор PostgreSQL в нижнем регистре")
        return value

    def url(self) -> URL:
        # URL.create экранирует пароль; в строку подключения и логи он не попадает.
        return URL.create(
            "postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


class Settings(DatabaseSettings):
    """Настройки сервиса. Ключ шлюза: файл `gateway_api_key` в каталоге секретов."""

    # Issuer токенов (значение claim iss) и адрес JWKS. На стенде JWKS берётся по внутреннему
    # адресу Keycloak, а iss остаётся публичным (hostname Keycloak).
    oidc_issuer: str
    oidc_jwks_url: str
    oidc_audience: str = "platform-api"
    # Клиенты (claim azp), чьи токены принимаются (ADR-0006, п. 3).
    allowed_clients: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["open-webui"])
    jwt_leeway_seconds: Annotated[int, Field(ge=0, le=120)] = 30
    jwks_cache_seconds: Annotated[int, Field(ge=30, le=86400)] = 600

    # OpenAI-совместимый адрес шлюза (LiteLLM) и логические модели, доступные клиентам.
    gateway_url: str = "http://litellm:4000/v1"
    gateway_api_key: SecretStr
    gateway_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 300.0
    models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["chat-default", "chat-fast"]
    )

    # PDP по AuthZEN (ADR-0009).
    policy_url: str = "http://cerbos:3592"
    policy_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 3.0
    # Агенты инсталляции (agents.json рендера манифеста, ADR-0010); пусто = агентов нет.
    agents_file: Path | None = None
    # Scope политик инсталляции (ADR-0008, п. 4); пусто = только базовые политики ядра.
    policy_scope: str | None = None

    max_request_bytes: Annotated[int, Field(ge=1024, le=64 * 1024 * 1024)] = 8 * 1024 * 1024
    log_level: str = "INFO"

    @field_validator("allowed_clients", "models", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_list(value)

    @field_validator("allowed_clients")
    @classmethod
    def _clients(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("нужен хотя бы один разрешённый клиент")
        for item in value:
            if not _CLIENT_ID_RE.fullmatch(item):
                raise ValueError(f"недопустимый client_id: {item!r}")
        return value

    @field_validator("models")
    @classmethod
    def _models(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("нужна хотя бы одна модель")
        for item in value:
            if not _MODEL_RE.fullmatch(item):
                raise ValueError(f"недопустимое имя модели: {item!r}")
        return value

    @field_validator("agents_file", mode="before")
    @classmethod
    def _agents_file(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("policy_scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _SCOPE_RE.fullmatch(value):
            raise ValueError(f"недопустимый scope политик: {value!r}")
        return value

    @field_validator("oidc_issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def model_set(self) -> frozenset[str]:
        return frozenset(self.models)
