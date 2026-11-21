# deploy/compose: локальный стенд

Локальный стенд antec.ai на Docker (Mac): базовый слой OpenBao 2.7.2 и PostgreSQL 18.7 с
pgvector 0.8.7 ([ADR-0017](../../docs/adr/0017-base-layer-openbao-postgres.md)) и Keycloak 26.8.2
с брокером Entra ID ([ADR-0007](../../docs/adr/0007-idp-and-agent-delegation.md)). Образы только по
digest из [`policy/allowed-versions.yaml`](../../policy/allowed-versions.yaml) (ADR-0004).

## Требования

- macOS с Docker Desktop (Compose v2 или новее); скрипты используют Связку ключей (`security`) и `python3`.
- Около 3 GB свободной памяти в VM Docker (OpenBao 256 MB, PostgreSQL 1 GB, Keycloak 1,5 GB).
- Свободный порт `127.0.0.1:8080` (Keycloak).

## Команды

Из корня репозитория:

```bash
deploy/compose/scripts/up.sh
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

Подключиться к PostgreSQL:

```bash
docker compose -f deploy/compose/compose.yaml exec -u postgres postgres psql
```

## Что внутри

| Сервис | Роль | Сеть | Память |
| --- | --- | --- | --- |
| `openbao` | Секреты, Raft-хранилище в томе `openbao-data` | `secrets` (internal) | 256 MB |
| `secrets-render` | Одноразовая выгрузка секретов из OpenBao в тома `postgres-secrets` и `keycloak-secrets` (профиль `bootstrap`) | `secrets` | |
| `postgres` | `localhost/antec/postgres:18.7-pgvector0.8.7`, БД `keycloak`, `litellm`, `openwebui`, `platform`, `dbos` | `data` (internal) | 1 GB |
| `keycloak` | `localhost/antec/keycloak:26.8.2`, realm `antec`, брокер `entra`; `http://localhost:8080` | `data`, `egress` | 1,5 GB |

Наружу опубликован только порт Keycloak `127.0.0.1:8080`. Выход в интернет есть только у Keycloak
(сеть `egress`, вход через Entra). Секретов нет в репозитории, на диске хоста, в окружении и argv
контейнеров.

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

При первом входе `antec-admin` и `breakglass` настраивают TOTP. Структуру realm меняют в
`keycloak/realm-antec.json` и применяют `up.sh --recreate-realm`, а не в админ-консоли.

Приложение в Entra ID (single-tenant): redirect URI (Web)
`http://localhost:8080/realms/antec/broker/entra/endpoint`, delegated `openid`, `profile`, `email`,
client secret со сроком не больше 180 дней, «Assignment required» с назначенной группой.

## Ограничения локального стенда

- TLS OpenBao выключен, одна доля ключа распечатывания. На сервере так нельзя (ADR-0017).
- Keycloak по HTTP на localhost; Admin API на том же адресе, что и вход. На сервере TLS через
  reverse proxy и отдельный адрес админ-консоли (ADR-0007).
- Логический бэкап без PITR.
- Перезапуск `postgres` напрямую через `docker compose` работает, но обновить секреты можно
  только через `up.sh`.
