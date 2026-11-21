"""Безопасная загрузка YAML: запрет дублей ключей и доступ к номерам строк."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


class YamlLoadError(Exception):
    def __init__(self, message: str, line: int | None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader, который падает на повторяющихся ключах вместо молчаливой перезаписи."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        # Проверяем только явные ключи узла. Ключи из merge (<<: *anchor) по спецификации
        # переопределяются явными, это не дубль.
        seen: dict[Hashable, int] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError as exc:
                raise ConstructorError(
                    "при разборе словаря",
                    node.start_mark,
                    "ключ нехешируемого типа",
                    key_node.start_mark,
                ) from exc
            line = key_node.start_mark.line + 1
            if key in seen:
                raise YamlLoadError(
                    f"повторяющийся ключ {key!r} (первое вхождение в строке {seen[key]})", line
                )
            seen[key] = line
        return super().construct_mapping(node, deep=deep)


def effective_items(node: MappingNode) -> dict[str, tuple[ScalarNode, Node]]:
    """Действующие пары словаря с учётом merge-ключей: явный ключ сильнее унаследованного.

    После конструирования PyYAML разворачивает `<<` прямо в node.value (унаследованные пары
    идут первыми), поэтому побеждает последнее вхождение ключа. Неразвёрнутые `<<`
    обрабатываются здесь же: из нескольких источников побеждает более ранний.
    """
    merged: dict[str, tuple[ScalarNode, Node]] = {}
    explicit: dict[str, tuple[ScalarNode, Node]] = {}
    for key_node, value_node in node.value:
        if key_node.tag == _MERGE_TAG:
            sources: list[Node] = (
                [value_node] if isinstance(value_node, MappingNode) else list(value_node.value)
            )
            for src in reversed(sources):
                if isinstance(src, MappingNode):
                    merged.update(effective_items(src))
        elif isinstance(key_node, ScalarNode):
            explicit[key_node.value] = (key_node, value_node)
    return {**merged, **explicit}


@dataclass(frozen=True, slots=True)
class LoadedYaml:
    data: Any
    root: Node | None

    def line_of(self, loc: Sequence[int | str]) -> int | None:
        """Номер строки (с 1) узла по пути вида ("components", "litellm", "allowed", 0)."""
        node = self.root
        best: int | None = node.start_mark.line + 1 if node is not None else None
        for part in loc:
            if node is None:
                break
            nxt: Node | None = None
            if isinstance(node, MappingNode):
                pair = effective_items(node).get(str(part))
                if pair is not None:
                    nxt = pair[1]
                    best = pair[0].start_mark.line + 1
            elif (
                isinstance(node, SequenceNode)
                and isinstance(part, int)
                and 0 <= part < len(node.value)
            ):
                nxt = node.value[part]
                best = nxt.start_mark.line + 1
            if nxt is None:
                break
            node = nxt
        return best


def load_yaml(text: str) -> LoadedYaml:
    loader = _StrictLoader(text)
    try:
        root = loader.get_single_node()
        data = loader.construct_document(root) if root is not None else None
    except YamlLoadError:
        raise
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark is not None else None
        problem = exc.problem or str(exc)
        raise YamlLoadError(f"некорректный YAML: {problem}", line) from exc
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError: например, невалидная дата 2026-13-01 в timestamp-конструкторе.
        raise YamlLoadError(f"некорректный YAML: {exc}", None) from exc
    finally:
        loader.dispose()
    return LoadedYaml(data=data, root=root)
