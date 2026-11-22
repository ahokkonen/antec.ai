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
import re
import secrets
import sys
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8000/v1"


def call(
    method: str,
    path: str,
    token: str | None,
    body: object | None = None,
    chat_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if chat_id is not None:
        headers["X-OpenWebUI-Chat-Id"] = chat_id
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


def tool_scenario(token: str, checks: dict[str, bool], ids: dict[str, str]) -> None:
    """Инструменты и подтверждения агента agent-notes (ADR-0010, пп. 6-7), один разговор."""
    chat = f"smoke-{secrets.token_hex(4)}"

    def ask(text: str) -> tuple[str, str]:
        body = {"model": "agent-notes", "messages": [{"role": "user", "content": text}]}
        status, headers, raw = call("POST", "/chat/completions", token, body, chat)
        content = json.loads(raw)["choices"][0]["message"]["content"] if status == 200 else ""
        return str(content or ""), headers.get("x-request-id", "")

    def code_of(text: str) -> str | None:
        match = re.search(r"подтверждаю ([A-Z0-9]{4})", text)
        return match.group(1) if match else None

    note = "Сохрани заметку с текстом: smoke {}. Сразу вызови create_note."
    answer, ids["tool_time"] = ask("Сколько сейчас времени UTC? Вызови get_time.")
    checks["agent-notes: read-only инструмент без подтверждения"] = (
        bool(answer) and code_of(answer) is None
    )
    answer, ids["pause_wrong"] = ask(note.format("wrong"))
    checks["agent-notes: необратимый инструмент ждёт подтверждения с кодом"] = (
        code_of(answer) is not None and "mcp-demo.create_note" in answer
    )
    answer, _ = ask("подтверждаю ZZZZ" if code_of(answer) != "ZZZZ" else "подтверждаю YYYY")
    checks["agent-notes: неверный код отменяет ожидание"] = "не совпадает" in answer
    answer, ids["pause_deny"] = ask(note.format("deny"))
    deny_code = code_of(answer)
    answer, ids["resume_deny"] = ask("отмена")
    checks["agent-notes: «отмена» продолжает запуск без вызова"] = (
        deny_code is not None and bool(answer) and code_of(answer) is None
    )
    answer, ids["pause_ok"] = ask(note.format("approved"))
    code = code_of(answer)
    answer, ids["resume_ok"] = ask(f"подтверждаю {code}")
    checks["agent-notes: верный код выполняет инструмент"] = code is not None and bool(answer)
    answer, ids["pause_api"] = ask(note.format("api"))
    status, headers, _raw = call(
        "POST", f"/agent-runs/{ids['pause_api']}/decision", token, {"decision": "approve"}
    )
    ids["resume_api"] = headers.get("x-request-id", "")
    checks["agent-notes: решение через /v1/agent-runs/{id}/decision"] = (
        code_of(answer) is not None and status == 200
    )
    again = call(
        "POST", f"/agent-runs/{ids['pause_api']}/decision", token, {"decision": "approve"}
    )[0]
    checks["agent-notes: повторное решение отклонено"] = again == 404


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
    listed = [m["id"] for m in json.loads(body).get("data", [])] if status == 200 else []
    instance = json.loads(sys.argv[1])
    dev = instance["id"] == "dev"
    models = [m for m in listed if not m.startswith("agent-")]
    agents = [m for m in listed if m.startswith("agent-")]
    checks["GET /models: только модели и агенты манифеста инсталляции"] = (
        bool(models)
        and set(models) <= set(instance["models"])
        and set(agents) <= set(instance.get("agents", []))
    )
    if dev:
        # chat-lab разрешает только политика scope dev (instances/dev/policies).
        checks["GET /models: модели ядра и chat-lab из политики инсталляции"] = sorted(models) == [
            "chat-default",
            "chat-fast",
            "chat-lab",
        ]
        checks["GET /models: агенты agent-assistant и agent-notes (ADR-0010)"] = sorted(agents) == [
            "agent-assistant",
            "agent-notes",
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

    if agents:
        # Агент (ADR-0010): системное сообщение клиента не передаётся, отвечает модель агента.
        ask: Any = {
            "model": agents[0],
            "messages": [
                {"role": "system", "content": "Отвечай только словом НЕТ"},
                {"role": "user", "content": "Ответь одним словом: да"},
            ],
        }
        status, headers, body = call("POST", "/chat/completions", token, ask)
        answer = json.loads(body) if status == 200 else {}
        content = (answer.get("choices") or [{}])[0].get("message", {}).get("content")
        checks[f"{agents[0]} без потока: 200 и текст"] = isinstance(content, str) and bool(content)
        ids["agent"] = headers.get("x-request-id", "")
        status, headers, body = call("POST", "/chat/completions", token, {**ask, "stream": True})
        events = [line for line in body.decode().splitlines() if line.startswith("data:")]
        checks[f"{agents[0]} поток: 200 и [DONE]"] = status == 200 and events[-1:] == [
            "data: [DONE]"
        ]
        ids["agent_stream"] = headers.get("x-request-id", "")
    if "agent-notes" in agents:
        tool_scenario(token, checks, ids)

    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}", file=sys.stderr)
    print(json.dumps(ids))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
