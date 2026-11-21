# deploy/compose: базовый слой

Локальный стенд antec.ai на Docker (Mac): OpenBao 2.7.2 и PostgreSQL 18.7 с pgvector 0.8.7.
Решение: [ADR-0017](../../docs/adr/0017-base-layer-openbao-postgres.md). Образы только по digest из
[`policy/allowed-versions.yaml`](../../policy/allowed-versions.yaml) (ADR-0004).

## Требования

- macOS с Docker Desktop (Compose v2 или новее); скрипты используют Связку ключей (`security`) и `python3`.
- Около 1,5 GB свободной памяти в VM Docker для этого слоя.

## Команды

Из корня репозитория:

```bash
deploy/compose/scripts/up.sh
```

Первый запуск инициализирует OpenBao, кладёт ключ распечатывания и root token в Связку ключей
(запись `antec-local`), генерирует пароли PostgreSQL в OpenBao и поднимает PostgreSQL.
Повторные запуски распечатывают OpenBao и используют существующие данные.

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
  контейнеры и сверяет с манифестом. Рабочий стенд не затрагивается.
- `down.sh` останавливает стенд и сохраняет данные; `down.sh --purge` удаляет тома и ключи из
  Связки ключей (с подтверждением).

Подключиться к PostgreSQL:

```bash
docker compose -f deploy/compose/compose.yaml exec -u postgres postgres psql
```

## Что внутри

| Сервис | Роль | Сеть | Память |
| --- | --- | --- | --- |
| `openbao` | Секреты, Raft-хранилище в томе `openbao-data` | `secrets` (internal) | 256 MB |
| `secrets-render` | Одноразовая выгрузка паролей из OpenBao в том `postgres-secrets` (профиль `bootstrap`) | `secrets` | |
| `postgres` | `localhost/antec/postgres:18.7-pgvector0.8.7`, БД `keycloak`, `litellm`, `openwebui`, `platform`, `dbos` | `data` (internal) | 1 GB |

Порты на хост не публикуются. Сети без выхода наружу. Секретов нет в репозитории, на диске хоста
и в окружении контейнеров.

## Ограничения локального стенда

- TLS OpenBao выключен, одна доля ключа распечатывания. На сервере так нельзя (ADR-0017).
- Логический бэкап без PITR.
- Перезапуск `postgres` напрямую через `docker compose` работает, но обновить секреты можно
  только через `up.sh`.
