"""Схема манифеста инсталляции `instance.yaml`, версия 1 (ADR-0008, п. 2).

Неизвестные поля = ошибка: опечатка в манифесте не должна молча менять поведение инсталляции.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from antec_instance.yamlload import YamlError, load_mapping

SCHEMA_VERSION = 1
MANIFEST_FILE = "instance.yaml"

# Переменные с ключами провайдеров, которые ядро выгружает из OpenBao в окружение LiteLLM
# (deploy/compose/openbao/render.sh). Другие имена LiteLLM не увидит.
PROVIDER_KEY_ENV = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"})
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2026-07-28")

# Те же ограничения, что в Platform API (antec_platform/settings.py).
_MODEL = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_CLIENT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# id инсталляции = scope Cerbos: один сегмент без точек (ADR-0008, допущение).
_INSTANCE_ID = r"^[a-z][a-z0-9-]{1,30}$"
_SLUG = r"^[a-z][a-z0-9-]{0,62}$"
_ENV_REF = re.compile(r"os\.environ/([A-Z][A-Z0-9_]*)")

InstanceId = Annotated[str, StringConstraints(pattern=_INSTANCE_ID)]
ModelName = Annotated[str, StringConstraints(pattern=_MODEL)]
ClientId = Annotated[str, StringConstraints(pattern=_CLIENT_ID)]
Slug = Annotated[str, StringConstraints(pattern=_SLUG)]
Text = Annotated[str, StringConstraints(max_length=200, pattern=r"^[^\r\n\x00]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _unique(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{what} {value!r} повторяется")
        seen.add(value)


def _check_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("ожидается адрес http(s)://хост[:порт][/путь]")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("в адресе не должно быть учётных данных, query и fragment")
    return value


class InstanceInfo(_Strict):
    id: InstanceId
    name: Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[^\r\n\x00]*$")]


class CoreRef(_Strict):
    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]


class LiteLLMParams(_Strict):
    """Белый список litellm_params: всё остальное (ключи в открытом виде, колбэки) запрещено."""

    model: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]+/[A-Za-z0-9._:/@-]+$")]
    api_key: str | None = None
    api_base: str | None = None
    api_version: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,32}$")] | None = None
    rpm: Annotated[int, Field(gt=0)] | None = None
    tpm: Annotated[int, Field(gt=0)] | None = None
    timeout: Annotated[float, Field(gt=0, le=1800)] | None = None

    @field_validator("api_key")
    @classmethod
    def _api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        match = _ENV_REF.fullmatch(value)
        if not match:
            raise ValueError("ключ только ссылкой os.environ/<ИМЯ>, не значением")
        if match.group(1) not in PROVIDER_KEY_ENV:
            known = ", ".join(sorted(PROVIDER_KEY_ENV))
            raise ValueError(f"переменная {match.group(1)} не выгружается ядром; доступны: {known}")
        return value

    @field_validator("api_base")
    @classmethod
    def _api_base(cls, value: str | None) -> str | None:
        return None if value is None else _check_url(value)


class Model(_Strict):
    name: ModelName
    description: Text = ""
    litellm: LiteLLMParams


class McpTool(_Strict):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    # Только манифест решает, что инструмент без подтверждения: подсказкам сервера
    # (readOnlyHint, destructiveHint) не доверяем (ADR-0008, п. 2).
    read_only: bool = False


class McpServer(_Strict):
    name: Slug
    url: str
    audience: ClientId
    protocol_version: Literal["2025-11-25", "2026-07-28"]
    tools: Annotated[list[McpTool], Field(min_length=1)]
    secrets: list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = []

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _check_url(value)

    @field_validator("tools")
    @classmethod
    def _tools(cls, value: list[McpTool]) -> list[McpTool]:
        _unique([t.name for t in value], "инструмент")
        return value

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        _unique(value, "секрет")
        return value


class Settings(_Strict):
    """Параметры Platform API, которые может менять инсталляция (ADR-0008, п. 2)."""

    allowed_clients: Annotated[list[ClientId], Field(min_length=1)] | None = None
    max_request_bytes: Annotated[int, Field(ge=1024, le=64 * 1024 * 1024)] | None = None
    gateway_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None

    @field_validator("allowed_clients")
    @classmethod
    def _clients(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            _unique(value, "клиент")
        return value


class Manifest(_Strict):
    schema_version: Literal[1]
    instance: InstanceInfo
    core: CoreRef
    models: Annotated[list[Model], Field(min_length=1)]
    mcp_servers: list[McpServer] = []
    settings: Settings = Settings()

    @model_validator(mode="before")
    @classmethod
    def _reserved(cls, data: Any) -> Any:  # noqa: ANN401 - сырые данные до проверки
        if isinstance(data, dict) and "agents" in data:
            raise ValueError("раздел agents зарезервирован: формат агентов задаст ADR-0010")
        return data

    @field_validator("models")
    @classmethod
    def _models(cls, value: list[Model]) -> list[Model]:
        _unique([m.name for m in value], "модель")
        return value

    @field_validator("mcp_servers")
    @classmethod
    def _servers(cls, value: list[McpServer]) -> list[McpServer]:
        _unique([s.name for s in value], "MCP-сервер")
        _unique([s.audience for s in value], "аудитория MCP-сервера")
        return value


class ManifestError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _location(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "(корень)"


def parse_manifest(data: object) -> Manifest:
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(
            [f"{_location(err['loc'])}: {err['msg']}" for err in exc.errors()]
        ) from exc


def load_manifest(instance_dir: Path) -> Manifest:
    path = instance_dir / MANIFEST_FILE
    try:
        data = load_mapping(path)
    except YamlError as exc:
        raise ManifestError([f"{MANIFEST_FILE}: {exc}"]) from exc
    return parse_manifest(data)
