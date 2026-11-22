"""Безопасная загрузка YAML: SafeLoader и запрет повторяющихся ключей."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode


class YamlError(Exception):
    """Файл не разобран: синтаксис, повторяющийся ключ или не тот тип верхнего уровня."""


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                line = key_node.start_mark.line + 1
                raise YamlError(f"строка {line}: повторяющийся ключ {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_all(path: Path) -> list[Any]:
    """Все документы файла (JSON тоже разбирается как YAML)."""
    try:
        text = path.read_text(encoding="utf-8")
        return list(yaml.load_all(text, Loader=_StrictLoader))
    except yaml.YAMLError as exc:
        raise YamlError(f"YAML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise YamlError(f"чтение: {exc}") from exc


def load_mapping(path: Path) -> dict[str, Any]:
    docs = load_all(path)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise YamlError("ожидается один документ YAML со словарём верхнего уровня")
    return docs[0]
