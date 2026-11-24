# Platform API

OpenAI-совместимый фасад antec.ai: проверка токена Keycloak, пользователи, история, вызовы
моделей и расходы, аудит, агенты. Решение и модель данных: [ADR-0006](../../docs/adr/0006-platform-api-data-model.md),
агенты: [ADR-0010](../../docs/adr/0010-agent-runtime.md).

## Что умеет v0.1

| Метод | Путь | Что делает |
| --- | --- | --- |
| `GET` | `/v1/models` | Логические модели (`PLATFORM_MODELS`) и агенты (`PLATFORM_AGENTS_FILE`) |
| `POST` | `/v1/chat/completions` | Чат с потоком SSE и без; в LiteLLM уходят только параметры OpenAI из белого списка, `user` = id пользователя Platform API |
| `POST` | `/v1/agent-runs/{id}/decision` | Решение по запуску агента, ждущему подтверждения (`approve` или `deny`), для адаптеров каналов |
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

Инструменты агентов (MCP-серверы манифеста, `PLATFORM_MCP_SERVERS_FILE`): свой MCP-клиент
2026-07-28 (`adapters/mcp.py`, типы `mcp-types`), перед вызовом `call` на `tool` в Cerbos, токен
пользователя меняется в Keycloak на токен с аудиторией сервера (`adapters/keycloak.py`,
`PLATFORM_OIDC_TOKEN_URL`, секрет `platform_oidc_client_secret`); аудит `tool.call.started` до вызова
(нет записи = нет вызова) и `tool.call` после, без аргументов, результата и текстов ошибок
([ADR-0012](../../docs/adr/0012-audit-retention-pii.md)). Всё, что не `read_only` в манифесте, только после подтверждения:
агент отвечает «подтверждаю <код>» или «отмена» (тот же разговор, `X-OpenWebUI-Chat-Id`), срок
`PLATFORM_APPROVAL_TTL_SECONDS`; решение пишется событием `tool.approval`, запуск продолжается
от сохранённого состояния (`agent_runs.state`, удаляется вместе с решением).

Классы данных ([ADR-0011](../../docs/adr/0011-data-classes-and-model-routing.md)): допуски моделей
`PLATFORM_MODELS_FILE`, класс ввода `PLATFORM_DEFAULT_DATA_CLASS`; класс разговора растёт с
каждым начатым вызовом инструмента выше класса ввода. Модель, агент или инструмент выше
допуска отклоняются до вызова (403 `data_class_forbidden`, аудит с причиной `data_class`).

Фоновый сервис (`antec-platform worker`, [ADR-0012](../../docs/adr/0012-audit-retention-pii.md), п. 2): роль
`platform_worker`, раз в `PLATFORM_WORKER_INTERVAL_SECONDS` (60) закрывает истёкшие ожидания, вызовы и
запуски в `started` дольше `PLATFORM_WORKER_ABANDON_SECONDS` (3600, причина `abandoned`) и вызовы
инструментов без итога (`tool.call.unknown`).

Очистка по срокам: тот же сервис раз в сутки (`PLATFORM_RETENTION_CONTENT_DAYS`,
`PLATFORM_RETENTION_METADATA_DAYS` из манифеста), включая чаты Open WebUI
(`PLATFORM_OPEN_WEBUI_DATABASE`); событие `retention.purge`. Права субъекта данных:
`antec-platform subject find|export|erase` ролью `platform_subject` (события `subject.export`,
`subject.erase`).

## Код

| Модуль | Роль |
| --- | --- |
| `app.py` | Маршруты фасада, поток SSE, журнал запросов |
| `auth.py` | JWKS с кэшем, проверка токена, `Principal` |
| `chat.py` | Белый список параметров, новые сообщения, разбор событий SSE |
| `worker.py` | Фоновые задачи: истечение ожиданий, потерянные вызовы (ADR-0012) |
| `retention.py` | Очистка по срокам хранения (ADR-0012, п. 1) |
| `subject.py` | Выгрузка и удаление данных субъекта (ADR-0012, п. 7) |
| `store.py` | Пользователи, разговоры, вызовы, аудит (PostgreSQL) |
| `db.py`, `migrations/` | Схема (SQLAlchemy Core) и миграции Alembic |
| `policy.py` | Контракт `PolicyClient`: субъект, действие, ресурсы |
| `agents.py` | Контракт `AgentRuntime`, описания агентов из `agents.json`, вход агента из сообщений |
| `tools.py` | MCP-серверы манифеста, `ToolBroker`: белый список, права, класс данных, обмен токена, аудит |
| `data_classes.py` | Классы данных и допуски моделей (ADR-0011) |
| `adapters/` | Контракт шлюза `ChatGateway`, адаптер LiteLLM и `PolicyClient` по AuthZEN (HTTP, без SDK), `AgentRuntime` на Pydantic AI |
| `settings.py` | Настройки: несекретные из `PLATFORM_*`, секреты из файлов `/run/antec-secrets` |

Секреты (файлы в `/run/antec-secrets`, имя = переменная в нижнем регистре):
`platform_database_password`, `platform_gateway_api_key`, `platform_oidc_client_secret`. На стенде их выгружает `secrets-render`
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

Таблица аудита после миграций `platform` принадлежит роли `platform_audit` (схема `audit`): её меняет
только `deploy/compose/postgres/audit.sql` (ADR-0012, п. 4).

Новая миграция: файл в `src/antec_platform/migrations/versions/` с `upgrade(*, app_role)`;
права роли сервиса выдаются в самой миграции. Тест `test_schema_matches_metadata` сверяет
миграции с `db.py`. На стенде миграции применяет `scripts/up.sh` (сервис `platform-migrate`,
роль владельца схемы `platform`).
