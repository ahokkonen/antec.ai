"""Производные файлы манифеста (ADR-0008, п. 3): LiteLLM, Platform API, сводка для скриптов.

Модели задаются только в манифесте; LiteLLM читает `litellm/models.yaml` через `include`,
Platform API получает `platform-api.env` и `platform-api/agents.json` (ADR-0010), скрипты
стенда читают `instance.json`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from antec_instance.agents import resolve_agents
from antec_instance.manifest import Manifest

# Каждому сервису свой подкаталог: контейнер монтирует только своё (LiteLLM не видит агентов).
LITELLM_MODELS = "litellm/models.yaml"
AGENTS = "platform-api/agents.json"
MCP_SERVERS = "platform-api/mcp_servers.json"
PLATFORM_ENV = "platform-api.env"
SUMMARY = "instance.json"
# Прежняя раскладка (до подкаталогов): удаляется, чтобы не осталось устаревших копий.
_LEGACY = ("litellm-models.yaml", "agents.json")
_HEADER = "# Сгенерировано tools/instance render из instance.yaml (ADR-0008). Не редактировать.\n"


def litellm_models(manifest: Manifest) -> dict[str, Any]:
    entries = []
    for model in manifest.models:
        entry: dict[str, Any] = {
            "model_name": model.name,
            "litellm_params": model.litellm.model_dump(exclude_none=True),
        }
        if model.description:
            entry["model_info"] = {"description": model.description}
        entries.append(entry)
    # Только model_list: другие ключи include заменили бы настройки ядра целиком.
    return {"model_list": entries}


def platform_env(manifest: Manifest) -> dict[str, str]:
    env = {
        "PLATFORM_MODELS": ",".join(m.name for m in manifest.models),
        "PLATFORM_POLICY_SCOPE": manifest.instance.id,
    }
    s = manifest.settings
    if s.allowed_clients is not None:
        env["PLATFORM_ALLOWED_CLIENTS"] = ",".join(s.allowed_clients)
    if s.max_request_bytes is not None:
        env["PLATFORM_MAX_REQUEST_BYTES"] = str(s.max_request_bytes)
    if s.gateway_timeout_seconds is not None:
        env["PLATFORM_GATEWAY_TIMEOUT_SECONDS"] = repr(s.gateway_timeout_seconds)
    if s.log_level is not None:
        env["PLATFORM_LOG_LEVEL"] = s.log_level
    return env


def summary(manifest: Manifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "id": manifest.instance.id,
        "core_version": manifest.core.version,
        "models": [m.name for m in manifest.models],
        "agents": [a.name for a in manifest.agents],
        # Аудитории token exchange: клиенты Keycloak для MCP-серверов (ADR-0010, п. 6).
        "mcp_audiences": [s.audience for s in manifest.mcp_servers],
    }


def mcp_servers(manifest: Manifest) -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "url": s.url,
            "audience": s.audience,
            "protocol_version": s.protocol_version,
            "tools": [{"name": t.name, "read_only": t.read_only} for t in s.tools],
        }
        for s in manifest.mcp_servers
    ]


def _write(path: Path, content: str) -> None:
    # Без изменений файл не трогается: хэши и время изменения стенда остаются прежними.
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except FileNotFoundError:
        pass
    # Атомарная замена: читатель (compose, LiteLLM) не увидит наполовину записанный файл.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render(manifest: Manifest, instance_dir: Path, out_dir: Path) -> list[Path]:
    agents = resolve_agents(manifest, instance_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        LITELLM_MODELS: _HEADER
        + yaml.safe_dump(litellm_models(manifest), sort_keys=False, allow_unicode=True),
        PLATFORM_ENV: _HEADER + "".join(f"{k}={v}\n" for k, v in platform_env(manifest).items()),
        SUMMARY: json.dumps(summary(manifest), ensure_ascii=False, indent=2) + "\n",
        # Всегда, даже пустой: Platform API читает файл при старте.
        AGENTS: json.dumps(agents, ensure_ascii=False, indent=2) + "\n",
        MCP_SERVERS: json.dumps(mcp_servers(manifest), ensure_ascii=False, indent=2) + "\n",
    }
    for legacy in _LEGACY:
        (out_dir / legacy).unlink(missing_ok=True)
    written = []
    for name, content in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, content)
        written.append(path)
    return written
