"""Проверка LiteLLM на стенде (ADR-0001, ADR-0018). Выполняется ВНУТРИ контейнера litellm.

Только стандартная библиотека: запускается интерпретатором образа LiteLLM через
scripts/litellm-smoke.sh, который передаёт мастер-ключ JSON-объектом через stdin.
Порт LiteLLM на хост не публикуется, поэтому проверка идёт на 127.0.0.1 внутри контейнера.

Проверяется:
- готовность и подключение к БД, список логических моделей;
- виртуальный ключ с бюджетом и списком моделей: разрешённая модель отвечает, запрещённая нет;
- бюджет конечного пользователя по полю `user` (OSS, /customer/*): после исчерпания отказ;
- учёт расходов по ключу и по конечному пользователю;
- отзыв ключа.

Вызовы моделей стоят денег: только chat-fast, max_tokens 64, бюджеты ключа и пользователя
не больше 0,05 USD. Без ключа провайдера проверки моделей пропускаются, код выхода 2.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any, cast

BASE = "http://127.0.0.1:4000"
type Json = dict[str, Any]


def call(
    method: str, path: str, key: str, body: Json | None = None, *, timeout: float = 120
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - только 127.0.0.1 внутри контейнера
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


class Checks:
    def __init__(self) -> None:
        self.failed = False

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""))
        self.failed |= not ok
        return ok


def chat(key: str, model: str, user: str | None = None) -> tuple[int, Any]:
    body: Json = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Ответь одним словом: да."}],
    }
    if user is not None:
        body["user"] = user
    return call("POST", "/v1/chat/completions", key, body)


def wait_spend(master: str, path: str, *, attempts: int = 20) -> float:
    """Расходы пишутся в БД пакетами (раз в несколько секунд): ждём ненулевого значения."""
    spend = 0.0
    for _ in range(attempts):
        status, info = call("GET", path, master)
        if status == 200 and isinstance(info, dict):
            raw = info.get("spend")
            if raw is None and isinstance(info.get("info"), dict):
                raw = info["info"].get("spend")
            spend = float(raw or 0)
            if spend > 0:
                return spend
        time.sleep(3)
    return spend


def main() -> int:
    master = cast(str, json.load(sys.stdin)["master_key"])
    c = Checks()
    suffix = secrets.token_hex(4)
    end_user = f"smoke-user-{suffix}"
    budget_user = f"smoke-budget-{suffix}"

    status, ready = call("GET", "/health/readiness", master)
    c.check(
        "готовность, БД подключена",
        status == 200 and isinstance(ready, dict) and ready.get("db") == "connected",
        ready,
    )
    status, models = call("GET", "/v1/models", master)
    ids = sorted(m["id"] for m in models.get("data", [])) if isinstance(models, dict) else []
    c.check("логические модели chat-default и chat-fast", ids == ["chat-default", "chat-fast"], ids)

    status, gen = call(
        "POST",
        "/key/generate",
        master,
        {
            "models": ["chat-fast"],
            "max_budget": 0.05,
            "duration": "15m",
            "key_alias": f"smoke-{suffix}",
            "metadata": {"purpose": "scripts/litellm-smoke.sh"},
        },
    )
    if not c.check("виртуальный ключ: только chat-fast, бюджет 0,05", status == 200, gen):
        return 1
    vkey = cast(str, gen["key"])

    status, denied = chat(vkey, "chat-default")
    c.check("модель вне списка ключа отклонена", status in (400, 401, 403), (status, denied))

    providers_ready = True
    status, resp = chat(vkey, "chat-fast", user=end_user)
    if status in (401, 500) and "api_key" in json.dumps(resp).lower():
        providers_ready = False
        print(
            "  [skip] ключ провайдера не задан (scripts/set-llm-key.sh): вызовы моделей пропущены"
        )
    elif c.check("вызов chat-fast по виртуальному ключу", status == 200, (status, resp)):
        usage = resp.get("usage") or {}
        c.check("ответ содержит usage", int(usage.get("total_tokens") or 0) > 0, usage)
        c.check(
            "расходы ключа учтены",
            wait_spend(master, f"/key/info?key={vkey}") > 0,
        )
        c.check(
            "расходы конечного пользователя учтены",
            wait_spend(master, f"/customer/info?end_user_id={end_user}") > 0,
        )

        status, cust = call(
            "POST", "/customer/new", master, {"user_id": budget_user, "max_budget": 0.0000001}
        )
        c.check("конечный пользователь с бюджетом 1e-7 USD", status == 200, cust)
        status, first = chat(vkey, "chat-fast", user=budget_user)
        c.check("первый вызов в пределах бюджета", status == 200, (status, first))
        wait_spend(master, f"/customer/info?end_user_id={budget_user}")
        status, second = chat(vkey, "chat-fast", user=budget_user)
        c.check(
            "вызов после исчерпания бюджета пользователя отклонён",
            # LiteLLM 1.108.8 отвечает 422 с error.type = budget_exceeded.
            status in (400, 403, 422, 429) and "budget_exceeded" in json.dumps(second),
            (status, second),
        )

    status, deleted = call("POST", "/key/delete", master, {"keys": [vkey]})
    c.check("ключ отозван", status == 200, deleted)
    status, after = call("GET", "/v1/models", vkey)
    c.check("отозванный ключ не принимается", status in (400, 401, 403), (status, after))
    call("POST", "/customer/delete", master, {"user_ids": [end_user, budget_user]})

    if c.failed:
        return 1
    return 0 if providers_ready else 2


if __name__ == "__main__":
    sys.exit(main())
