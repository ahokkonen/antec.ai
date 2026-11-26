"""Проверки файлов: docker-compose, Dockerfile, uv.lock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pincheck.policy import ComponentIndex, Policy


@dataclass(frozen=True, slots=True)
class CoreImage:
    """Образ ядра antec.ai из core-images.yaml инсталляции (ADR-0014, п. 6)."""

    tag: str
    digest: str
    source: str


@dataclass(frozen=True, slots=True)
class CheckContext:
    policy: Policy
    index: ComponentIndex
    policy_path: str
    require_digest: bool
    # Нормализованный репозиторий -> закреплённый тег и digest (--core-images).
    core_images: Mapping[str, CoreImage] = field(default_factory=dict)
