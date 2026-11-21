#!/usr/bin/env bash
# Первичная инициализация кластера (выполняется entrypoint образа один раз, на пустом PGDATA).
# Отдельная БД, роль и лимит соединений на каждый сервис (ADR-0003).
# Пароли читаются из файлов, которые secrets-render выгрузил из OpenBao; в аргументы
# командной строки и в логи они не попадают.
set -euo pipefail

SECRETS_DIR=/run/antec-secrets

# сервис:лимит_соединений
SERVICES=(
  "keycloak:50"
  "litellm:50"
  "openwebui:30"
  "platform:50"
  "dbos:20"
)

for entry in "${SERVICES[@]}"; do
  svc="${entry%%:*}"
  limit="${entry##*:}"
  pw_file="${SECRETS_DIR}/${svc}"
  if [[ ! -s "${pw_file}" ]]; then
    echo "нет пароля для роли ${svc}: ${pw_file}" >&2
    exit 1
  fi
  # Пароль читается внутри сессии psql (\set из файла), поэтому не попадает в argv
  # процессов и в логи. Имена сервисов фиксированы списком выше.
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres \
       -v svc="${svc}" -v lim="${limit}" <<SQL
\\set pw \`cat ${pw_file}\`
CREATE ROLE :"svc" LOGIN PASSWORD :'pw' CONNECTION LIMIT :lim;
CREATE DATABASE :"svc" OWNER :"svc";
REVOKE ALL ON DATABASE :"svc" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"svc" TO :"svc";
SQL
  psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${svc}" -v svc="${svc}" <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO :"svc";
SQL
done

# Векторный поиск нужен только Platform API (RAG).
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname platform <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL

echo "antec: созданы БД и роли: ${SERVICES[*]%%:*}"
