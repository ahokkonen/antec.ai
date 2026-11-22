"""Контракт проверки прав (ADR-0009): кто может выполнить действие над ресурсом.

Реализация по AuthZEN в adapters/authzen.py. Любой сбой PDP = PolicyUnavailable, и фасад
отвечает 503 (отказ PDP = запрет, а не разрешение).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class PolicyUnavailable(Exception):  # noqa: N818
    """PDP недоступен или ответ не разобран: решения нет, действие запрещено."""


@dataclass(frozen=True, slots=True)
class Subject:
    """Кто действует: пользователь Platform API с ролями из токена."""

    id: str
    roles: tuple[str, ...]
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Resource:
    kind: str
    id: str
    # Атрибуты ресурса для политик (например, data_class, ADR-0011).
    attrs: dict[str, str] = field(default_factory=dict)


class PolicyClient(Protocol):
    async def check(self, subject: Subject, action: str, resources: list[Resource]) -> list[bool]:
        """Решение по каждому ресурсу в том же порядке; пустой список ресурсов = пустой ответ."""

    async def ping(self) -> None: ...

    async def aclose(self) -> None: ...
