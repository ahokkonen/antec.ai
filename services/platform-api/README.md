# Platform API

OpenAI-совместимый фасад antec.ai: проверка токена Keycloak, пользователи, история, вызовы
моделей и расходы, аудит, агенты. Решение и модель данных: [ADR-0006](../../docs/adr/0006-platform-api-data-model.md),
агенты: [ADR-0010](../../docs/adr/0010-agent-runtime.md).

## Что умеет v0.1

| Метод | Путь | Что делает |
| --- | --- | --- |
| `GET` | `/v1/models` | Логические модели (`PLATFORM_MODELS`) и агенты (`PLATFORM_AGENTS_FILE`) |
| `POST` | `/v1/chat/completions` | Чат с потоком SSE и без; в LiteLLM уходят только параметры OpenAI из белого списка, `user` = id пользователя Platform API |
| `GET` | `/healthz` | Процесс жив |
| `GET` | `/readyz` | БД и JWKS доступны |

Аутентификация: `Authorization: Bearer <access token Keycloak>` с `aud: platform-api` и `azp` из
`PLATFORM_ALLOWED_CLIENTS`. Права ([ADR-0009](../../docs/adr/0009-authorization-cerbos.md)):
`invoke` на `model` проверяется в Cerbos по AuthZEN (`PLATFORM_POLICY_URL`) с ролями из claim
`platform_roles`; `/v1/models` отдаёт только разрешённые модели; отказ PDP = 503. Scope политик
инсталляции (`PLATFORM_POLICY_SCOPE`, [ADR-0008](../../docs/adr/0008-plugin-contract-and-instance-template.md))
уходит в каждом ресурсе как `cerbos.scope`; `PLATFORM_MODELS` и `PLATFORM_POLICY_SCOPE` задаёт манифест
инсталляции (`tools/instance render`). Ошибки в формате OpenAI, у каждого ответа заголовок `x-request-id`
(он же id вызова в `model_calls` и `audit_events`).

Агенты (`agent-<имя>`, `agents.json` рендера манифеста): вызов требует `invoke` на `agent` и на
его модель; инструкции задаёт манифест, системные сообщения и параметры генерации клиента не
передаются; модель агента вызывается в LiteLLM тем же ключом с `user` = id пользователя. Запуск
записывается в `agent_runs` (событие `agent.run`), каждый шаг в `model_calls` с `agent_run_id`
(событие `model.call`). Рантайм: Pydantic AI только в `adapters/pydantic_ai.py` за контрактом
`AgentRuntime` (`agents.py`); граница импорта проверяется тестом `test_boundaries.py`.

## Код

| Модуль | Роль |
| --- | --- |
| `app.py` | Маршруты фасада, поток SSE, журнал запросов |
| `auth.py` | JWKS с кэшем, проверка токена, `Principal` |
| `chat.py` | Белый список параметров, новые сообщения, разбор событий SSE |
| `store.py` | Пользователи, разговоры, вызовы, аудит (PostgreSQL) |
| `db.py`, `migrations/` | Схема (SQLAlchemy Core) и миграции Alembic |
| `policy.py` | Контракт `PolicyClient`: субъект, действие, ресурсы |
| `agents.py` | Контракт `AgentRuntime`, описания агентов из `agents.json`, вход агента из сообщений |
| `adapters/` | Контракт шлюза `ChatGateway`, адаптер LiteLLM и `PolicyClient` по AuthZEN (HTTP, без SDK), `AgentRuntime` на Pydantic AI |
| `settings.py` | Настройки: несекретные из `PLATFORM_*`, секреты из файлов `/run/antec-secrets` |

Секреты (файлы в `/run/antec-secrets`, имя = переменная в нижнем регистре):
`platform_database_password`, `platform_gateway_api_key`. На стенде их выгружает `secrets-render`
из OpenBao (`antec/platform-api`, `antec/platform-migrate`).

## Разработка

Нужен [uv](https://docs.astral.sh/uv/). Из этого каталога:

```bash
uv sync --locked
```

```bash
uv run --locked ruff format --check . && uv run --locked ruff check . && uv run --locked mypy
```

```bash
uv run --locked pytest
```

Тест с настоящим Cerbos и политиками `policies/cerbos` (маркер `cerbos`) запускается, если задан
`PLATFORM_TEST_CERBOS_URL` (CI поднимает Cerbos по digest из реестра).

Тесты на PostgreSQL (маркер `db`) запускаются, если задан `PLATFORM_TEST_DATABASE_URL` с
суперпользователем; тест создаёт и удаляет свои роли и БД. Например, с одноразовым контейнером
по digest из реестра:

```bash
docker run -d --rm --name antec-platform-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test docker.io/library/postgres:18.7@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a
```

```bash
PLATFORM_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:55432/postgres uv run --locked pytest
```

Зависимости обновляются только PR: `exclude-newer` в `pyproject.toml` сдвигается не ближе
7 дней к дате обновления, новые версии проверяются по лицензии и OSV (ADR-0004, ADR-0016).

## Миграции

Новая миграция: файл в `src/antec_platform/migrations/versions/` с `upgrade(*, app_role)`;
права роли сервиса выдаются в самой миграции. Тест `test_schema_matches_metadata` сверяет
миграции с `db.py`. На стенде миграции применяет `scripts/up.sh` (сервис `platform-migrate`,
роль владельца схемы `platform`).
