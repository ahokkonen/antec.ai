# Общие функции скриптов стенда. Подключается через source, не запускается напрямую.
# shellcheck shell=bash
set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYCHAIN_ACCOUNT="antec-local"
# Ключи OpenBao хранятся по идентификатору установки: после `down.sh --purge` и новой
# установки ключи старой остаются в Связке ключей, и её бэкапы по-прежнему восстанавливаются.
KC_INSTALLATION="antec-local-installation"     # идентификатор текущей установки
KC_LEGACY_UNSEAL="antec-local-openbao-unseal-key"
KC_LEGACY_ROOT="antec-local-openbao-root-token"
kc_unseal_name() { echo "antec-local-openbao-unseal-key-$1"; }
kc_root_name() { echo "antec-local-openbao-root-token-$1"; }

compose() {
  docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yaml" "$@"
}

log() { printf '\033[1m[antec]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[antec] ошибка:\033[0m %s\n' "$*" >&2; exit 1; }

require_macos_keychain() {
  command -v security >/dev/null 2>&1 || die "нужна Связка ключей macOS (команда security); стенд рассчитан на macOS"
}

# Чтение секрета из Связки ключей. Пусто, если записи нет.
kc_get() {
  security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$1" -w 2>/dev/null || true
}

# Запись секрета: значение передаётся через stdin `security -i`, не через argv.
kc_set() {
  local service="$1" value="$2"
  [[ "$value" =~ ^[A-Za-z0-9._+/=-]+$ ]] || die "неожиданные символы в значении для $service"
  printf 'add-generic-password -U -a %s -s %s -w %s\n' "$KEYCHAIN_ACCOUNT" "$service" "$value" \
    | security -i >/dev/null
}

kc_delete() {
  security delete-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$1" >/dev/null 2>&1 || true
}

# Идентификатор текущей установки. Записи прежнего формата (без идентификатора)
# переносятся под новый идентификатор один раз.
current_installation() {
  local id; id="$(kc_get "$KC_INSTALLATION")"
  if [[ -z "$id" && -n "$(kc_get "$KC_LEGACY_UNSEAL")" ]]; then
    id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    kc_set "$(kc_unseal_name "$id")" "$(kc_get "$KC_LEGACY_UNSEAL")"
    kc_set "$(kc_root_name "$id")" "$(kc_get "$KC_LEGACY_ROOT")"
    kc_set "$KC_INSTALLATION" "$id"
    kc_delete "$KC_LEGACY_UNSEAL"
    kc_delete "$KC_LEGACY_ROOT"
    log "ключи OpenBao перенесены в формат с идентификатором установки ($id)"
  fi
  echo "$id"
}

# Команда bao внутри контейнера openbao. Токен (если нужен) передаётся через окружение
# docker CLI (`-e BAO_TOKEN` без значения), а не в argv.
bao_exec() {
  compose exec -T -e BAO_TOKEN openbao bao "$@"
}

# Статус OpenBao: 0 распечатан, 2 запечатан, иное = недоступен.
bao_status_code() {
  local rc=0
  compose exec -T openbao bao status -format=json >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

wait_for_openbao_process() {
  local i
  for i in $(seq 1 30); do
    local rc; rc="$(bao_status_code)"
    if [[ "$rc" == "0" || "$rc" == "2" ]]; then return 0; fi
    sleep 1
  done
  die "OpenBao не отвечает; смотрите: docker compose logs openbao"
}

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

DATABASES=(keycloak litellm openwebui platform dbos)
BACKUP_ROOT="${ANTEC_BACKUP_DIR:-$HOME/Library/Application Support/antec/backups}"
POSTGRES_IMAGE="localhost/antec/postgres:18.7-pgvector0.8.7"

# psql от пользователя postgres внутри контейнера (peer-аутентификация по сокету).
pg_exec() {
  compose exec -T -u postgres postgres "$@"
}

# Число строк по каждой пользовательской таблице БД, JSON-объект.
ROWCOUNT_SQL="select coalesce(json_object_agg(n.nspname || '.' || c.relname,
  (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname),
  false, true, '')))[1]::text::bigint order by n.nspname, c.relname), '{}')
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where c.relkind in ('r', 'p') and n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname not like 'pg_toast%'"

# Входные данные scripts/keycloak.py (JSON на stdout): секреты antec/keycloak и, если задан,
# antec/entra (tenant_id, client_id; client_secret не передаётся, он в vault-файле Keycloak).
# Нужен BAO_TOKEN с правом чтения обоих путей.
keycloak_inputs() {
  # Оба документа идут через stdin одного python3, а не через argv.
  { bao_exec kv get -mount=antec -format=json keycloak
    bao_exec kv get -mount=antec -format=json entra 2>/dev/null || echo null
  } | python3 -c '
import json, sys
text, dec, docs, pos = sys.stdin.read(), json.JSONDecoder(), [], 0
while len(docs) < 2:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    doc, pos = dec.raw_decode(text, pos)
    docs.append(doc)
kc = docs[0]["data"]["data"]
entra = docs[1]["data"]["data"] if docs[1] else None
kc["entra"] = {k: entra[k] for k in ("tenant_id", "client_id")} if entra else None
print(json.dumps(kc))'
}
