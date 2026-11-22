"""PolicyClient по AuthZEN Authorization API 1.0 (ADR-0009). PDP v1: Cerbos 0.57.

Особенности Cerbos известны только здесь: роли передаются плоским свойством
`subject.properties["cerbos.roles"]` (так читает internal/svc/authzen_svc.go 0.57.0; вложенный
объект из документации не работает), остальные свойства становятся атрибутами принципала.
Без ролей Cerbos подставляет роль = `subject.type`, поэтому пустые роли сюда не доходят.
Пакетная проверка: одна `/access/v1/evaluations`, общие subject и action, ресурсы в `evaluations`.
Scope инсталляции (ADR-0008, п. 4) передаётся плоским свойством ресурса
`resource.properties["cerbos.scope"]` (`toResource` в том же файле Cerbos).
"""

from __future__ import annotations

from typing import Any

import httpx

from antec_platform.policy import PolicyUnavailable, Resource, Subject

# Лимит Cerbos по умолчанию (server.requestLimits.maxResourcesPerRequest).
_MAX_BATCH = 50


class AuthZenPolicyClient:
    def __init__(self, base_url: str, timeout: float, scope: str | None = None) -> None:
        self._scope = scope
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> None:
        try:
            resp = await self._client.get(".well-known/authzen-configuration")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PolicyUnavailable(f"PDP недоступен: {type(exc).__name__}") from exc

    @staticmethod
    def _subject(subject: Subject) -> dict[str, Any]:
        return {
            "type": "user",
            "id": subject.id,
            "properties": {**subject.attrs, "cerbos.roles": list(subject.roles)},
        }

    def _resource(self, resource: Resource) -> dict[str, Any]:
        out: dict[str, Any] = {"type": resource.kind, "id": resource.id}
        if self._scope:
            out["properties"] = {"cerbos.scope": self._scope}
        return out

    async def check(self, subject: Subject, action: str, resources: list[Resource]) -> list[bool]:
        if not resources:
            return []
        # Без ролей принципал недопустим для Cerbos; без ролей прав нет по определению.
        if not subject.roles:
            return [False] * len(resources)
        out: list[bool] = []
        for start in range(0, len(resources), _MAX_BATCH):
            out += await self._batch(subject, action, resources[start : start + _MAX_BATCH])
        return out

    async def _batch(self, subject: Subject, action: str, resources: list[Resource]) -> list[bool]:
        body = {
            "subject": self._subject(subject),
            "action": {"name": action},
            "evaluations": [{"resource": self._resource(r)} for r in resources],
        }
        try:
            resp = await self._client.post("access/v1/evaluations", json=body)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PolicyUnavailable(f"PDP: {type(exc).__name__}") from exc
        evaluations = data.get("evaluations") if isinstance(data, dict) else None
        if not isinstance(evaluations, list) or len(evaluations) != len(resources):
            raise PolicyUnavailable("PDP: ответ не соответствует запросу")
        decisions: list[bool] = []
        for item in evaluations:
            decision = item.get("decision") if isinstance(item, dict) else None
            if not isinstance(decision, bool):
                raise PolicyUnavailable("PDP: в ответе нет решения")
            decisions.append(decision)
        return decisions
