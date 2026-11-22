"""Агенты манифеста с текстом инструкций (ADR-0010, п. 3): то, что получает Platform API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from antec_instance.manifest import MAX_INSTRUCTIONS_BYTES, Manifest, ManifestError


def _read_instructions(instance_dir: Path, rel: str, agent: str) -> str:
    root = instance_dir.resolve()
    path = (instance_dir / rel).resolve()
    # Только файлы внутри репозитория инсталляции, в том числе после символических ссылок.
    if not path.is_relative_to(root):
        raise ManifestError([f"агент {agent}: instructions_file вне каталога инсталляции"])
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManifestError([f"агент {agent}: {rel}: {exc.strerror}"]) from exc
    if len(data) > MAX_INSTRUCTIONS_BYTES:
        raise ManifestError([f"агент {agent}: {rel} больше {MAX_INSTRUCTIONS_BYTES} байт"])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError([f"агент {agent}: {rel}: не UTF-8"]) from exc
    if not text.strip():
        raise ManifestError([f"агент {agent}: {rel}: пустой файл"])
    return text


def resolve_agents(manifest: Manifest, instance_dir: Path) -> list[dict[str, Any]]:
    out = []
    for agent in manifest.agents:
        instructions = agent.instructions
        if agent.instructions_file is not None:
            instructions = _read_instructions(instance_dir, agent.instructions_file, agent.name)
        out.append(
            {
                "name": agent.name,
                "description": agent.description,
                "model": agent.model,
                "instructions": instructions,
                "tools": list(agent.tools),
                "max_steps": agent.max_steps,
                "max_tool_calls": agent.max_tool_calls,
            }
        )
    return out
