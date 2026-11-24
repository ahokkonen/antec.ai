# deploy/compose: локальный стенд

Локальный стенд antec.ai на Docker (Mac): базовый слой OpenBao 2.7.2 и PostgreSQL 18.7 с
pgvector 0.8.7 ([ADR-0017](../../docs/adr/0017-base-layer-openbao-postgres.md)) и Keycloak 26.8.2
с брокером Entra ID ([ADR-0007](../../docs/adr/0007-idp-and-agent-delegation.md)), LLM-шлюз LiteLLM
1.108.8 и веб-чат Open WebUI 0.13.3 ([ADR-0018](../../docs/adr/0018-litellm-on-base-layer.md)), Platform API
([ADR-0006](../../docs/adr/0006-platform-api-data-model.md), [services/platform-api](../../services/platform-api/README.md)) и
PDP Cerbos 0.57.0 ([ADR-0009](../../docs/adr/0009-authorization-cerbos.md), политики [policies/cerbos](../../policies/cerbos)). Модели, политики и
настройки инсталляции задаёт её манифест ([ADR-0008](../../docs/adr/0008-plugin-contract-and-instance-template.md)), по умолчанию
dev-инсталляция ядра [`instances/dev`](../../instances/dev/instance.yaml). Образы только по
digest из [`policy/allowed-versions.yaml`](../../policy/allowed-versions.yaml) (ADR-0004).

## Требования

- macOS с Docker Desktop (Compose v2 или новее); скрипты используют Связку ключей (`security`), `python3`
  и `uv` (рендер манифеста инсталляции).
- Около 6,5 GB свободной памяти в VM Docker (лимиты: OpenBao 256 MB, PostgreSQL 1 GB, Keycloak,
  LiteLLM и Open WebUI по 1,5 GB, Platform API 512 MB, Cerbos и platform-worker по 256 MB; фактически около 2,8 GB).
- Свободные порты `127.0.0.1:8080` (Keycloak) и `127.0.0.1:3000` (Open WebUI).

## Команды

Из корня репозитория:

```bash
deploy/compose/scripts/up.sh
```

Инсталляция: `ANTEC_INSTANCE_DIR` (по умолчанию `instances/dev`). `up.sh` сначала проверяет её
манифест и политики и пишет производные файлы в `<инсталляция>/.rendered/` (`tools/instance render`):
модели LiteLLM (`include` в `litellm/config.yaml`), `PLATFORM_MODELS`, `PLATFORM_POLICY_SCOPE` и
настройки Platform API, агенты (`agents.json`, ADR-0010), MCP-серверы (`mcp_servers.json`), допуски моделей по классам
данных (`models.json`, ADR-0011), модели ключа
`platform-api` в LiteLLM. По `mcp_servers` манифеста `up.sh` создаёт в Keycloak клиенты-аудитории
для обмена токена, а сервисы плагинов из `compose.override.yaml` собирает и запускает в сети `mcp`. `compose.override.yaml` инсталляции,
если есть, подключается к `compose.yaml`. Репозиторий инсталляции с ядром в `core/`:

```bash
ANTEC_INSTANCE_DIR="$PWD" core/deploy/compose/scripts/up.sh
```

Первый запуск инициализирует OpenBao, кладёт ключ распечатывания и root token в Связку ключей
(аккаунт `antec-local`, записи с идентификатором установки), генерирует пароли PostgreSQL и секреты
Keycloak в OpenBao, поднимает PostgreSQL и Keycloak и создаёт realm `antec` из
[`keycloak/realm-antec.json`](keycloak/realm-antec.json).
Повторные запуски распечатывают OpenBao, используют существующие данные и приводят к заданному
состоянию секреты клиентов, профиль пользователя и брокер Entra. `up.sh --recreate-realm` удаляет
realm `antec` и создаёт его заново (пользователи realm удаляются).

```bash
deploy/compose/scripts/set-entra.sh
```

Спрашивает Tenant ID, Client ID и client secret приложения Entra ID (секрет вводится без эха),
сохраняет их в OpenBao (`antec/entra`), перенастраивает стенд и перезапускает Keycloak. Без этого
шага вход возможен только локальными пользователями.

```bash
deploy/compose/scripts/backup.sh
```

```bash
deploy/compose/scripts/restore-verify.sh
```

```bash
deploy/compose/scripts/down.sh
```

- `backup.sh` пишет бэкап в `~/Library/Application Support/antec/backups/<время UTC>/`
  (переопределяется `ANTEC_BACKUP_DIR`). Каталог чувствительный: права 700.
- `restore-verify.sh` восстанавливает последний (или указанный) бэкап в одноразовые изолированные
  контейнеры и сверяет с манифестом: строки, канарейка, снапшот OpenBao (ключом установки из
  манифеста), вход каждой роли в свою БД по SCRAM с паролем из восстановленного OpenBao.
  Рабочий стенд не затрагивается.
- `down.sh` останавливает стенд и сохраняет данные.
- `down.sh --purge` удаляет тома стенда. Ключи OpenBao этой установки остаются в Связке ключей,
  поэтому её бэкапы по-прежнему восстанавливаются.
- `down.sh --purge --forget-keys` удаляет и ключи: снапшоты OpenBao из бэкапов этой установки
  станут невосстановимыми.

Проверить вход и обмен токенов (RFC 8693): откроется браузер, после входа скрипт печатает claims
токена пользователя и токена для `mcp-m365` и результат проверок.

```bash
bash -c 'source deploy/compose/scripts/lib.sh && export BAO_TOKEN="$(kc_get "$(kc_root_name "$(current_installation)")")" && keycloak_inputs | python3 deploy/compose/scripts/keycloak.py smoke'
```

```bash
deploy/compose/scripts/set-llm-key.sh anthropic
```

Сохраняет ключ API провайдера (`anthropic`, `openai` или `openrouter`) в OpenBao
(`antec/llm-providers`, ввод без эха) и перезапускает LiteLLM. У провайдера должен стоять лимит
расходов. Без ключа LiteLLM запускается, но модели не отвечают.

```bash
deploy/compose/scripts/litellm-smoke.sh
```

Проверка LiteLLM изнутри контейнера: модели, виртуальный ключ с бюджетом и списком моделей,
бюджет конечного пользователя, учёт расходов, отзыв ключа. Вызовы `chat-fast` стоят доли цента.
Код выхода 2: ключ провайдера не задан, вызовы моделей пропущены.

```bash
deploy/compose/scripts/platform-smoke.sh
```

Проверка Platform API: откроется браузер для входа (клиент `antec-smoke`), затем изнутри
контейнера `platform-api`: модели (`chat-lab` разрешает только политика scope `dev`), агент
`agent-assistant` с потоком и без (ADR-0010; в БД: `agent_runs`, шаги в `model_calls`, история, аудит),
агент `agent-notes` с инструментами `mcp-demo`: read-only вызов, пауза с кодом, неверный код, отмена,
подтверждение в чате и через `/v1/agent-runs/{id}/decision` (аудит `tool.call`, `tool.approval`), агент
`agent-lab` на `chat-lab` (допуск `internal`): `create_note` (`confidential`) отклоняется до вызова (ADR-0011), отказ без токена и с подделанной подписью, чат `chat-fast` с
потоком и без (с параметрами шлюза, которые фасад должен отбросить), неизвестная модель; в БД:
статус и токены вызовов, вопрос и ответ в истории, запись аудита, конечный пользователь в учёте
LiteLLM = пользователь Platform API. Токен передаётся только через конвейер.

```bash
deploy/compose/scripts/policies-check.sh
```

Компиляция и тесты политик Cerbos ядра и инсталляции вместе (аргумент: каталог инсталляции, по
умолчанию `instances/dev`), тем же образом, что на стенде.

Подключиться к PostgreSQL:

```bash
docker compose -f deploy/compose/compose.yaml exec -u postgres postgres psql
```

## Что внутри

| Сервис | Роль | Сеть | Память |
| --- | --- | --- | --- |
| `openbao` | Секреты, Raft-хранилище в томе `openbao-data` | `secrets` (internal) | 256 MB |
| `secrets-render` | Одноразовая выгрузка секретов из OpenBao в тома сервисов (профиль `bootstrap`) | `secrets` | |
| `postgres` | `localhost/antec/postgres:18.7-pgvector0.8.7`, БД `keycloak`, `litellm`, `openwebui`, `platform`, `dbos` | `data` (internal) | 1 GB |
| `keycloak` | `localhost/antec/keycloak:26.8.2`, realm `antec`, брокер `entra`, клиенты-аудитории MCP из манифеста; `http://localhost:8080` | `data`, `egress`, `ui`, `mcp` | 1,5 GB |
| `litellm` | `litellm-non_root:v1.108.8`, модели из манифеста инсталляции (dev: `chat-default`, `chat-fast`, `chat-lab`); порт 4000 только внутри | `data`, `llm` (internal), `llm-egress` | 1,5 GB |
| `cerbos` | `cerbos:0.57.0`, политики ядра `/policies/core` и инсталляции `/policies/instance` (scope, только чтение); без телеметрии, Admin API и аудита | `policy` (internal) | 256 MB |
| `platform-migrate` | Одноразовые миграции БД `platform` ролью владельца схемы (профиль `bootstrap`) | `data` | 256 MB |
| `platform-api` | `localhost/antec/platform-api:0.1.0`, фасад `/v1` (модели и агенты инсталляции, ADR-0010), роль БД `platform_app`; порт 8000 только внутри | `data`, `llm`, `platform`, `policy`, `mcp` (internal) | 512 MB |
| `platform-worker` | Тот же образ, команда `worker` (ADR-0012): истёкшие ожидания и потерянные вызовы, роль БД `platform_worker` | `data` | 256 MB |
| `open-webui` | `open-webui:v0.13.3`, вход через Keycloak; `http://localhost:3000` | `data`, `platform`, `ui` | 1,5 GB |
| `mcp-demo` | Плагин dev-инсталляции (`instances/dev/plugins/mcp-demo`, `compose.override.yaml`): тестовый MCP-сервер 2026-07-28, токен по JWKS Keycloak | `mcp` (internal) | 128 MB |

Наружу опубликованы только порты Keycloak `127.0.0.1:8080` и Open WebUI `127.0.0.1:3000` (сеть
`ui`). Выход в интернет нужен Keycloak (вход через Entra) и LiteLLM (API провайдеров); у Open WebUI
он есть только потому, что Docker публикует порт из не-internal сети (`OFFLINE_MODE` включён). Секретов нет в репозитории, на диске хоста, в окружении и argv
контейнеров.

## Open WebUI

http://localhost:3000, вход только через Keycloak (Entra или `breakglass`). Роль в Open WebUI
задаёт Keycloak при каждом входе: роль `user` клиента `open-webui` есть у всех, `admin` назначается
в админ-консоли Keycloak (Clients → open-webui → Roles, сейчас только у `breakglass`).
Администратор Open WebUI не видит чаты пользователей; сессия 8 часов. `up.sh` сверяет аккаунты
Open WebUI с Keycloak: создаёт `breakglass` в пустом Open WebUI (иначе первый вошедший стал бы
администратором), снимает роль admin с аккаунтов без роли `admin` в Keycloak и переносит аккаунты,
созданные по `sub`, на `antec_uid`. Локальным пользователям Keycloak `antec_uid` = логин
(заполняется `up.sh`).

Единственное подключение Open WebUI: фасад Platform API с access token пользователя из Keycloak
(`auth_type: system_oauth`, ADR-0006); ключа LiteLLM у Open WebUI нет, прежний ключ `open-webui`
`up.sh` удаляет. Platform API ходит в LiteLLM ключом `platform-api` и передаёт `user` = id
пользователя Platform API; каждый пользователь получает бюджет `platform-user` (5 USD на 30 дней).
Доступ к моделям решает Platform API по политикам Cerbos (ADR-0009): права моделей в Open WebUI
отключены (`BYPASS_MODEL_ACCESS_CONTROL`), список моделей фильтрует фасад. Роли Platform API:
роли клиента `platform-api` в Keycloak (`platform-user` у всех по умолчанию, `platform-admin`
явно, Clients → platform-api → Roles), в токене claim `platform_roles`. Политики меняются в
`policies/cerbos` (ядро) и `policies/` инсталляции (scope = id инсталляции) и применяются `up.sh`
(перезапуск Cerbos по хэшу).
Сессия Open WebUI (8 часов) не длиннее SSO-сессии Keycloak: простой до 8 часов, максимум
10 часов от начала сессии Keycloak (`ssoSessionIdleTimeout`, `ssoSessionMaxLifespan` в
`keycloak/realm-antec.json`; `up.sh` применяет их и к существующему realm). Блокировка
пользователя или снятие роли действуют не позже чем через 15 минут (срок access token). Если
сессия Keycloak закончилась раньше сессии Open WebUI (выход, администратор, вход в Open WebUI по
сессии Keycloak старше 2 часов), Open WebUI не видит моделей: выйдите из Open WebUI и войдите
заново. Настройки задаются только окружением в `compose.yaml`:
изменения в админке Open WebUI не переживают перезапуск.

## Keycloak

| Что | Где |
| --- | --- |
| Вход пользователей (account console) | http://localhost:8080/realms/antec/account |
| Администрирование | http://localhost:8080/admin, пользователь `antec-admin` (realm `master`) |
| Аварийный вход | пользователь `breakglass` (realm `antec`) |
| Пароли `antec-admin` и `breakglass` | OpenBao `antec/keycloak`: `admin_password`, `breakglass_password` |
| Клиенты realm | `platform-api` (token exchange V2), `open-webui`, `mcp-m365` (аудитория), `antec-smoke` (только стенд) |

Прочитать пароль (печатается в терминал):

```bash
bash -c 'source deploy/compose/scripts/lib.sh && export BAO_TOKEN="$(kc_get "$(kc_root_name "$(current_installation)")")" && bao_exec kv get -mount=antec -field=breakglass_password keycloak'
```

Вход по паролю в обоих realm всегда требует OTP (browser flow `antec browser`): при первом
входе `antec-admin` и `breakglass` настраивают TOTP, а после удаления OTP Keycloak требует
настроить его заново. Структуру realm меняют в
`keycloak/realm-antec.json` и применяют `up.sh --recreate-realm`, а не в админ-консоли.

Приложение в Entra ID (single-tenant): redirect URI (Web)
`http://localhost:8080/realms/antec/broker/entra/endpoint`, delegated `openid`, `profile`, `email`,
client secret со сроком не больше 180 дней, «Assignment required» с назначенной группой.

## Ограничения локального стенда

- TLS OpenBao выключен, одна доля ключа распечатывания. На сервере так нельзя (ADR-0017).
- `up.sh` перезапускает работающий сервис, если изменились его секреты или смонтированные файлы
  конфигурации (`litellm/config.yaml`, `common/env-from-files.sh`). Запуск `docker compose` в обход
  скриптов не знает их хэшей и пересоздаст LiteLLM и Open WebUI; используйте `up.sh`.
- LiteLLM: секреты в окружении процесса (Vault в LiteLLM платный), выход в интернет не ограничен
  доменами провайдеров, админка выключена (ADR-0018).
- Keycloak по HTTP на localhost; Admin API на том же адресе, что и вход. На сервере TLS через
  reverse proxy и отдельный адрес админ-консоли (ADR-0007).
- Логический бэкап без PITR.
- Сеть `data` общая для всех сервисов с БД: Open WebUI видит по ней LiteLLM и Platform API.
  Доступ закрывают ключи и токены (у Open WebUI ключа LiteLLM нет), а не сеть; отдельные сети
  к PostgreSQL на сервис: открытый вопрос ADR-0006.
- Перезапуск `postgres` напрямую через `docker compose` работает, но обновить секреты можно
  только через `up.sh`.
