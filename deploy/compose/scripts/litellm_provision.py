"""Подготовка LiteLLM для клиентов стенда (ADR-0018, ADR-0006). Выполняется в контейнере litellm.

Только стандартная библиотека: запускается интерпретатором образа LiteLLM из scripts/up.sh,
секреты и модели приходят JSON-объектом через stdin ({"master_key", "platform_api_key", "models"});
модели = логические модели манифеста инсталляции (ADR-0008).

Идемпотентно создаёт:
- бюджет `platform-user`: бюджет каждого конечного пользователя Platform API на 30 дней
  (конечный пользователь = поле `user` = id пользователя Platform API);
- виртуальный ключ Platform API со значением из OpenBao (antec/platform-api:litellm_key):
  только логические модели инсталляции, конечные пользователи без своего бюджета получают
  `platform-user`.

Существующие объекты приводятся к заданным: сумма и период бюджета, модели и бюджет ключа.
Ключ `open-webui` (прямое подключение Open WebUI до Platform API, ADR-0018, п. 7) удаляется.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

BASE = "http://127.0.0.1:4000"
PLATFORM_ALIAS = "platform-api"
RETIRED_ALIASES = ["open-webui"]
END_USER_BUDGET_ID = "platform-user"
END_USER_BUDGET_USD = 5.0
END_USER_BUDGET_DURATION = "30d"

type Json = dict[str, Any]


class ProvisionError(RuntimeError):
    pass


def call(method: str, path: str, key: str, body: Json | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - только 127.0.0.1 внутри контейнера
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def ok(status: int, payload: object, what: str) -> None:
    if status != 200:
        raise ProvisionError(f"{what}: HTTP {status}: {payload}")


def log(msg: str) -> None:
    print(f"litellm: {msg}", file=sys.stderr)


def ensure_budget(master: str) -> None:
    """Создаёт бюджет или приводит сумму и период существующего к заданным.

    /budget/info отвечает 200 и пустым списком, если бюджета нет; любой другой ответ считается
    ошибкой, а не отсутствием (иначе повторное создание упадёт на дубликате).
    """
    status, info = call("POST", "/budget/info", master, {"budgets": [END_USER_BUDGET_ID]})
    if status != 200 or not isinstance(info, list):
        raise ProvisionError(f"проверка бюджета {END_USER_BUDGET_ID}: HTTP {status}: {info}")
    wanted: Json = {
        "budget_id": END_USER_BUDGET_ID,
        "max_budget": END_USER_BUDGET_USD,
        "budget_duration": END_USER_BUDGET_DURATION,
    }
    if not info:
        log(f"бюджет {END_USER_BUDGET_ID}: {END_USER_BUDGET_USD} USD на {END_USER_BUDGET_DURATION}")
        status, payload = call("POST", "/budget/new", master, wanted)
        ok(status, payload, f"создание бюджета {END_USER_BUDGET_ID}")
        return
    current = cast(Json, info[0])
    if (
        float(current.get("max_budget") or 0) != END_USER_BUDGET_USD
        or current.get("budget_duration") != END_USER_BUDGET_DURATION
    ):
        log(
            f"бюджет {END_USER_BUDGET_ID} приведён к {END_USER_BUDGET_USD} USD "
            f"на {END_USER_BUDGET_DURATION}"
        )
        status, payload = call("POST", "/budget/update", master, wanted)
        ok(status, payload, f"обновление бюджета {END_USER_BUDGET_ID}")


def ensure_platform_key(master: str, key: str, models: list[str]) -> None:
    query = urllib.parse.urlencode({"key": key})
    status, info = call("GET", f"/key/info?{query}", master)
    wanted: Json = {
        "models": models,
        "key_alias": PLATFORM_ALIAS,
        "metadata": {"purpose": "Platform API: служебный ключ фасада (ADR-0006)"},
        "end_user_budget_id": END_USER_BUDGET_ID,
    }
    if status == 200 and isinstance(info, dict):
        current = cast(Json, info.get("info") or {})
        same = (
            sorted(current.get("models") or []) == sorted(models)
            and (
                current.get("end_user_budget_id")
                or (current.get("metadata") or {}).get("end_user_budget_id")
            )
            == END_USER_BUDGET_ID
        )
        if not same:
            log("ключ Platform API: модели и бюджет пользователей приведены к заданным")
            status, payload = call("POST", "/key/update", master, {"key": key, **wanted})
            ok(status, payload, "обновление ключа Platform API")
        return
    if status not in (400, 401, 404):
        raise ProvisionError(f"проверка ключа Platform API: HTTP {status}: {info}")
    log("создание виртуального ключа Platform API (значение из OpenBao)")
    status, payload = call("POST", "/key/generate", master, {"key": key, **wanted})
    ok(status, payload, "создание ключа Platform API")


def retire_keys(master: str) -> None:
    """Удаляет ключи клиентов, которые больше не ходят в LiteLLM напрямую."""
    query = urllib.parse.urlencode(
        {"key_alias": ",".join(RETIRED_ALIASES), "return_full_object": "true"}
    )
    status, info = call("GET", f"/key/list?{query}", master)
    if status != 200 or not isinstance(info, dict):
        raise ProvisionError(f"список ключей: HTTP {status}: {info}")
    found = [
        k
        for k in cast(list[Any], info.get("keys") or [])
        if isinstance(k, dict) and k.get("key_alias") in RETIRED_ALIASES
    ]
    if not found:
        return
    log(f"удаление ключей: {', '.join(sorted({str(k['key_alias']) for k in found}))}")
    status, payload = call(
        "POST", "/key/delete", master, {"keys": [str(k["token"]) for k in found]}
    )
    ok(status, payload, "удаление прежних ключей")


def main() -> int:
    try:
        inputs = json.load(sys.stdin)
        master = cast(str, inputs["master_key"])
        ensure_budget(master)
        models = inputs["models"]
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(m, str) for m in models)
        ):
            raise ProvisionError("models: ожидается непустой список логических моделей")
        ensure_platform_key(master, cast(str, inputs["platform_api_key"]), models)
        retire_keys(master)
    except (ProvisionError, KeyError, ValueError, OSError) as exc:
        print(f"litellm: ошибка подготовки: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
