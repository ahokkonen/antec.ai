"""Схема манифеста инсталляции `instance.yaml` (ADR-0008, п. 2), версии 1-5.

Версия 2 добавляет раздел `agents` (ADR-0010, п. 3), версия 3 классы данных: `data`,
`max_data_class` моделей и `data_class` инструментов (ADR-0011), версия 4 сроки хранения
(ADR-0012), версия 5 комплаенс: `compliance` и `provider_terms` моделей (ADR-0015). Прежние
версии валидны.

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

SCHEMA_VERSION = 5
# Классы данных ядра по возрастанию (ADR-0011, п. 1).
DATA_CLASSES = ("public", "internal", "confidential", "restricted")
DataClass = Literal["public", "internal", "confidential", "restricted"]
AGENT_PREFIX = "agent-"
# Инструкции агента: в agents.json и в каждый запрос к модели, поэтому размер ограничен.
MAX_INSTRUCTIONS_BYTES = 32 * 1024
MANIFEST_FILE = "instance.yaml"

# Переменные с ключами провайдеров, которые ядро выгружает из OpenBao в окружение LiteLLM
# (deploy/compose/openbao/render.sh). Другие имена LiteLLM не увидит.
PROVIDER_KEY_ENV = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"})
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2026-07-28")
# Ревизии, которые понимает MCP-клиент ядра (ADR-0010, п. 6); публикуются в release.json.
CORE_MCP_PROTOCOL_VERSIONS = ("2026-07-28",)
# Схемы манифеста этой версии ядра и устаревшие из них (ADR-0014, п. 3): устаревшая схема
# удаляется не раньше чем через 2 minor после объявления; v1-v4 устарели в 0.1.0.
SCHEMA_VERSIONS = (1, 2, 3, 4, 5)
DEPRECATED_SCHEMA_VERSIONS = (1, 2, 3, 4)
DEPRECATED_SCHEMA_REMOVAL = "0.3.0"

# Те же ограничения, что в Platform API (antec_platform/settings.py).
_MODEL = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_CLIENT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# id инсталляции = scope Cerbos: один сегмент без точек (ADR-0008, допущение).
_INSTANCE_ID = r"^[a-z][a-z0-9-]{1,30}$"
_SLUG = r"^[a-z][a-z0-9-]{0,62}$"
_ENV_REF = re.compile(r"os\.environ/([A-Z][A-Z0-9_]*)")

_AGENT = r"^agent-[a-z0-9][a-z0-9-]{0,56}$"
_TOOL_REF = r"^[a-z][a-z0-9-]{0,62}\.[A-Za-z0-9_.-]{1,128}$"

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
    # Без кавычек и обратной косой черты: имя подставляется в YAML и Markdown шаблона.
    name: Annotated[
        str, StringConstraints(min_length=1, max_length=100, pattern=r'^[^\r\n\x00"\\]*$')
    ]


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


def class_rank(value: str) -> int:
    return DATA_CLASSES.index(value)


class ProviderTerms(_Strict):
    """Условия договора инсталляции с провайдером модели (ADR-0015, п. 12)."""

    # Провайдер не хранит промпты и ответы (zero data retention) по договору инсталляции.
    zdr: bool = False
    # Где обрабатываются данные этой модели по договору; unknown = не выяснено.
    region: Literal["eu", "us", "unknown"] = "unknown"


class Model(_Strict):
    name: ModelName
    description: Text = ""
    litellm: LiteLLMParams
    # Верхний допуск модели (ADR-0011, п. 2); в v1 облачным моделям разрешено всё.
    max_data_class: DataClass = "restricted"
    provider_terms: ProviderTerms | None = None

    @property
    def provider(self) -> str:
        """Провайдер = префикс litellm.model (anthropic/..., openai/..., openrouter/...)."""
        return self.litellm.model.split("/", 1)[0]

    @field_validator("name")
    @classmethod
    def _not_agent(cls, value: str) -> str:
        if value.startswith(AGENT_PREFIX):
            raise ValueError(f"префикс {AGENT_PREFIX!r} зарезервирован за агентами (ADR-0010)")
        return value


class Agent(_Strict):
    """Агент инсталляции (ADR-0010, п. 3). Инструкции: текст или файл в репозитории инсталляции."""

    name: Annotated[str, StringConstraints(pattern=_AGENT)]
    description: Text = ""
    model: ModelName
    instructions: Annotated[str, StringConstraints(min_length=1)] | None = None
    instructions_file: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")] | None
    ) = None
    tools: list[Annotated[str, StringConstraints(pattern=_TOOL_REF)]] = []
    max_steps: Annotated[int, Field(ge=1, le=50)] = 10
    max_tool_calls: Annotated[int, Field(ge=0, le=100)] = 20

    @model_validator(mode="after")
    def _instructions(self) -> Agent:
        if (self.instructions is None) == (self.instructions_file is None):
            raise ValueError("нужно ровно одно из instructions и instructions_file")
        if (
            self.instructions is not None
            and len(self.instructions.encode()) > MAX_INSTRUCTIONS_BYTES
        ):
            raise ValueError(f"instructions больше {MAX_INSTRUCTIONS_BYTES} байт")
        if self.instructions_file is not None and ".." in self.instructions_file.split("/"):
            raise ValueError("instructions_file: путь внутри репозитория инсталляции, без ..")
        return self

    @field_validator("tools")
    @classmethod
    def _tools(cls, value: list[str]) -> list[str]:
        _unique(value, "инструмент")
        return value


class McpTool(_Strict):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    # Только манифест решает, что инструмент без подтверждения: подсказкам сервера
    # (readOnlyHint, destructiveHint) не доверяем (ADR-0008, п. 2).
    read_only: bool = False
    # Класс данных, которые инструмент возвращает модели; пусто = data.default_class.
    data_class: DataClass | None = None


# Аудитория MCP-сервера = клиент Keycloak, созданный для обмена токена (ADR-0010, п. 6).
# Префикс mcp- не даёт выбрать аудиторией клиента ядра (open-webui, platform-api и др.).
McpAudience = Annotated[str, StringConstraints(pattern=r"^mcp-[a-z0-9][a-z0-9-]{0,59}$")]


class McpServer(_Strict):
    name: Slug
    url: str
    audience: McpAudience
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


class DataSettings(_Strict):
    """Классы данных инсталляции (ADR-0011, п. 2)."""

    # Класс всего, что вводит пользователь (до классификатора содержимого).
    default_class: DataClass = "internal"


class Retention(_Strict):
    """Сроки хранения, дни (ADR-0012, п. 1). Значения подтверждает DPIA (ADR-0015)."""

    # Тексты сообщений и чаты Open WebUI.
    content_days: Annotated[int, Field(ge=30, le=180)] = 90
    # Вызовы моделей, запуски агентов, пустые разговоры.
    metadata_days: Annotated[int, Field(ge=180, le=730)] = 365
    # Аудит, расходы LiteLLM, события Keycloak.
    audit_days: Annotated[int, Field(ge=180, le=730)] = 365


# Код языка интерфейса Open WebUI: с регионом (fi-FI), иначе перевод не находится.
_LOCALE = r"^[a-z]{2}-[A-Z]{2}$"
NoticeText = Annotated[
    str, StringConstraints(min_length=1, max_length=500, pattern=r"^[^\r\n\x00]*$")
]


class Party(_Strict):
    name: Text
    contact: Text


class Compliance(_Strict):
    """Роли и документы комплаенса инсталляции (ADR-0015)."""

    # controller: организация инсталляции сама контролёр (Mtech у себя); processor: Mtech
    # обработчик для клиента-контролёра (ADR-0015, п. 1).
    operator_role: Literal["controller", "processor"] = "controller"
    controller: Party | None = None
    processor: Party | None = None
    dpo_contact: Text | None = None
    privacy_notice_url: str | None = None
    # Свой текст уведомления об ИИ по языкам интерфейса (ст. 50(1)); иначе текст ядра.
    ai_notice: dict[Annotated[str, StringConstraints(pattern=_LOCALE)], NoticeText] = {}
    # Каждая модель обязана иметь provider_terms.zdr: true (ADR-0015, вопрос 4).
    require_zdr: bool = False

    @field_validator("privacy_notice_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if urlsplit(value).scheme != "https":
            raise ValueError("privacy_notice_url: только https")
        return _check_url(value)

    @model_validator(mode="after")
    def _roles(self) -> Compliance:
        if self.operator_role == "processor" and (
            self.controller is None or self.processor is None
        ):
            raise ValueError("operator_role processor: нужны controller (клиент) и processor")
        return self


def _v3_fields(data: dict[str, Any]) -> list[str]:
    found = ["data"] if "data" in data else []
    for i, model in enumerate(data.get("models") or []):
        if isinstance(model, dict) and "max_data_class" in model:
            found.append(f"models.{i}.max_data_class")
    for i, server in enumerate(data.get("mcp_servers") or []):
        for j, tool in enumerate(
            (server or {}).get("tools") or [] if isinstance(server, dict) else []
        ):
            if isinstance(tool, dict) and "data_class" in tool:
                found.append(f"mcp_servers.{i}.tools.{j}.data_class")
    return found


class Manifest(_Strict):
    schema_version: Literal[1, 2, 3, 4, 5]  # = SCHEMA_VERSIONS
    instance: InstanceInfo
    core: CoreRef
    models: Annotated[list[Model], Field(min_length=1)]
    mcp_servers: list[McpServer] = []
    agents: list[Agent] = []
    data: DataSettings = DataSettings()
    retention: Retention = Retention()
    compliance: Compliance = Compliance()
    settings: Settings = Settings()

    @model_validator(mode="before")
    @classmethod
    def _version_fields(cls, data: Any) -> Any:  # noqa: ANN401 - сырые данные до проверки
        if not isinstance(data, dict):
            return data
        version = data.get("schema_version")
        if "agents" in data and version == 1:
            raise ValueError("раздел agents есть только в schema_version: 2 и выше (ADR-0010)")
        if version in (1, 2) and (fields := _v3_fields(data)):
            raise ValueError(
                f"классы данных ({', '.join(fields)}) есть только в schema_version: 3 "
                "и выше (ADR-0011)"
            )
        if "retention" in data and version in (1, 2, 3):
            raise ValueError("раздел retention есть только в schema_version: 4 и выше (ADR-0012)")
        has_terms = any(
            isinstance(m, dict) and "provider_terms" in m for m in data.get("models") or []
        )
        if version in (1, 2, 3, 4) and ("compliance" in data or has_terms):
            raise ValueError(
                "compliance и provider_terms есть только в schema_version: 5 (ADR-0015)"
            )
        return data

    @model_validator(mode="after")
    def _zdr(self) -> Manifest:
        if self.compliance.require_zdr:
            missing = [
                m.name for m in self.models if not (m.provider_terms and m.provider_terms.zdr)
            ]
            if missing:
                raise ValueError(
                    f"compliance.require_zdr: у моделей {', '.join(missing)} нет "
                    "provider_terms.zdr: true"
                )
        return self

    def compliance_warnings(self, providers: dict[str, Any]) -> list[str]:
        """Что не помешает запуску, но нужно закрыть до DPIA (ADR-0015)."""
        if self.schema_version < 5:
            return ["schema_version < 5: роли и условия провайдеров не заданы (ADR-0015)"]
        out = []
        c = self.compliance
        if c.controller is None:
            out.append("compliance.controller не задан: реестр обработки без контролёра")
        placeholders = [
            f"compliance.{name}"
            for name, value in (
                ("controller.contact", c.controller.contact if c.controller else None),
                ("processor.contact", c.processor.contact if c.processor else None),
                ("dpo_contact", c.dpo_contact),
                ("privacy_notice_url", c.privacy_notice_url),
            )
            if value and re.search(r"example\.|\.invalid\b|\.test\b", value)
        ]
        if placeholders:
            out.append(f"{', '.join(placeholders)}: адрес-заглушка из шаблона")
        if c.privacy_notice_url is None:
            out.append("compliance.privacy_notice_url не задан: баннер без ссылки (ст. 13)")
        for m in self.models:
            if m.provider not in providers:
                out.append(
                    f"модель {m.name}: провайдера {m.provider} нет в policy/llm-providers.yaml"
                )
            if m.provider_terms is None or not m.provider_terms.zdr:
                out.append(f"модель {m.name}: без ZDR провайдер может хранить данные (ADR-0015)")
        return out

    def tool_class(self, server: str, tool: str) -> str:
        for s in self.mcp_servers:
            for t in s.tools:
                if s.name == server and t.name == tool:
                    return t.data_class or self.data.default_class
        raise KeyError(f"{server}.{tool}")

    def class_warnings(self) -> list[str]:
        """Что заведомо не будет работать по классам данных (ADR-0011, п. 3)."""
        models = {m.name: m for m in self.models}
        out = []
        default_rank = class_rank(self.data.default_class)
        for m in self.models:
            if class_rank(m.max_data_class) < default_rank:
                out.append(
                    f"модель {m.name} (допуск {m.max_data_class}) недоступна при "
                    f"data.default_class {self.data.default_class}"
                )
        for a in self.agents:
            limit = models[a.model].max_data_class
            for ref in a.tools:
                server, _, tool = ref.partition(".")
                cls = self.tool_class(server, tool)
                if class_rank(cls) > class_rank(limit):
                    out.append(
                        f"агент {a.name}: инструмент {ref} ({cls}) выше допуска модели "
                        f"{a.model} ({limit}) и будет отклоняться"
                    )
        return out

    @model_validator(mode="after")
    def _agent_refs(self) -> Manifest:
        models = {m.name for m in self.models}
        tools = {f"{s.name}.{t.name}" for s in self.mcp_servers for t in s.tools}
        problems = []
        for agent in self.agents:
            if agent.model not in models:
                problems.append(f"агент {agent.name}: модели {agent.model!r} нет в models")
            problems += [
                f"агент {agent.name}: инструмента {ref!r} нет в mcp_servers"
                for ref in agent.tools
                if ref not in tools
            ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @field_validator("agents")
    @classmethod
    def _agents(cls, value: list[Agent]) -> list[Agent]:
        _unique([a.name for a in value], "агент")
        return value

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
