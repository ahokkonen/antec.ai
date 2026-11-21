"""Проверки файлов: docker-compose, Dockerfile, uv.lock."""

from __future__ import annotations

from dataclasses import dataclass

from pincheck.policy import ComponentIndex, Policy


@dataclass(frozen=True, slots=True)
class CheckContext:
    policy: Policy
    index: ComponentIndex
    policy_path: str
    require_digest: bool
