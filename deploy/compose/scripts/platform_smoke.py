"""Проверка фасада Platform API (ADR-0006). Выполняется ВНУТРИ контейнера platform-api.

Только стандартная библиотека. Access token пользователя приходит через stdin (scripts/
platform-smoke.sh получает его входом через браузер, клиент antec-smoke). На stdout последней
строкой JSON с идентификаторами запросов: по ним platform-smoke.sh сверяет записи в БД.
Первый аргумент: instance.json инсталляции (ADR-0008), из него id и логические модели.
Вызовы chat-fast (или первой доступной модели) стоят доли цента.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8000/v1"


def call(
    method: str, path: str, token: str | None, body: object | None = None
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310 - только 127.0.0.1 внутри контейнера
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return int(resp.status), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main() -> int:
    token = sys.stdin.read().strip()
    if not token:
        print("platform-smoke: нет токена на stdin", file=sys.stderr)
        return 2
    checks: dict[str, bool] = {}
    ids: dict[str, str] = {}

    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    checks["роль platform-user в токене (platform_roles)"] = "platform-user" in claims.get(
        "platform_roles", []
    )
    status, _, body = call("GET", "/models", token)
    models = [m["id"] for m in json.loads(body).get("data", [])] if status == 200 else []
    instance = json.loads(sys.argv[1])
    dev = instance["id"] == "dev"
    checks["GET /models: только модели манифеста инсталляции"] = bool(models) and set(
        models
    ) <= set(instance["models"])
    if dev:
        # chat-lab разрешает только политика scope dev (instances/dev/policies).
        checks["GET /models: модели ядра и chat-lab из политики инсталляции"] = sorted(models) == [
            "chat-default",
            "chat-fast",
            "chat-lab",
        ]
    model = "chat-fast" if "chat-fast" in models else (models or ["chat-fast"])[0]
    status, headers, _ = call("GET", "/models", None)
    checks["без токена: 401 и WWW-Authenticate"] = status == 401 and "Bearer" in headers.get(
        "www-authenticate", ""
    )
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload}.{sig[:-4]}{'AAAA' if not sig.endswith('AAAA') else 'BBBB'}"
    checks["подделанная подпись: 401"] = call("GET", "/models", forged)[0] == 401

    question: Any = {
        "model": model,
        "messages": [{"role": "user", "content": "Ответь одним словом: да"}],
        "max_tokens": 5,
        # Параметры шлюза в обход белого списка: должны быть отброшены, а не переданы.
        "api_base": "http://127.0.0.1:9",
        "user": "spoofed-user",
    }
    status, headers, body = call("POST", "/chat/completions", token, question)
    usage = json.loads(body).get("usage") if status == 200 else None
    checks[f"{model} без потока: 200 и usage"] = isinstance(usage, dict)
    ids["completion"] = headers.get("x-request-id", "")

    if dev:
        lab = {**question, "model": "chat-lab"}
        checks["chat-lab (политика инсталляции): 200"] = (
            call("POST", "/chat/completions", token, lab)[0] == 200
        )

    status, headers, body = call("POST", "/chat/completions", token, {**question, "stream": True})
    events = [line for line in body.decode().splitlines() if line.startswith("data:")]
    checks[f"{model} поток: 200 и [DONE]"] = status == 200 and events[-1:] == ["data: [DONE]"]
    ids["stream"] = headers.get("x-request-id", "")

    status, _, _ = call("POST", "/chat/completions", token, {**question, "model": "gpt-unknown"})
    checks["неизвестная модель: 404"] = status == 404

    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}", file=sys.stderr)
    print(json.dumps(ids))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
