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

# Инсталляция (ADR-0008): манифест, политики и сервисы плагинов. Порядок: ANTEC_INSTANCE_DIR
# (репозиторий инсталляции задаёт свой корень, ядро в core/), затем инсталляция последнего
# up.sh (ACTIVE_INSTANCE_FILE), затем dev-инсталляция ядра. Так down.sh, backup.sh и smoke без
# переменной работают с той инсталляцией, что запущена.
ACTIVE_INSTANCE_FILE="$COMPOSE_DIR/.active-instance"
if [[ -z "${ANTEC_INSTANCE_DIR:-}" && -f "$ACTIVE_INSTANCE_FILE" ]]; then
  ANTEC_INSTANCE_DIR="$(cat "$ACTIVE_INSTANCE_FILE")"
fi
ANTEC_INSTANCE_DIR="$(cd "${ANTEC_INSTANCE_DIR:-$COMPOSE_DIR/../../instances/dev}" 2>/dev/null && pwd)" \
  || { printf '[antec] ошибка: нет каталога инсталляции %s\n' "${ANTEC_INSTANCE_DIR:-}" >&2; exit 1; }
ANTEC_INSTANCE_RENDERED="$ANTEC_INSTANCE_DIR/.rendered"
export ANTEC_INSTANCE_DIR

# Проверка манифеста и производные файлы (tools/instance render): модели LiteLLM, env Platform
# API, сводка instance.json. Нужен uv на хосте.
render_instance() {
  command -v uv >/dev/null 2>&1 || die "нужен uv (https://docs.astral.sh/uv/) для рендера манифеста инсталляции"
  uv run --quiet --project "$COMPOSE_DIR/../../tools/instance" --locked antec-instance \
    render "$ANTEC_INSTANCE_DIR" --out "$ANTEC_INSTANCE_RENDERED" >/dev/null \
    || die "манифест инсталляции $ANTEC_INSTANCE_DIR не прошёл проверку"
  compute_config_shas
}

# Запомнить инсталляцию, с которой запущен стенд (вызывает up.sh).
remember_active_instance() {
  printf '%s\n' "$ANTEC_INSTANCE_DIR" > "$ACTIVE_INSTANCE_FILE"
}

# Логические модели инсталляции из instance.json (JSON-массив).
instance_models_json() {
  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["models"]))' \
    "$ANTEC_INSTANCE_RENDERED/instance.json"
}

# Хэши смонтированных файлов конфигурации. Compose не замечает изменения содержимого
# bind-mount, поэтому хэш передаётся в окружение сервиса (compose.yaml): изменился файл,
# изменилось окружение, compose up пересоздаёт контейнер. Отсутствующие файлы (рендер ещё не
# выполнялся) не учитываются: up.sh пересчитывает хэши после render_instance.
config_sha() { # файлы...
  local f
  for f in "$@"; do
    if [[ -f "$f" ]]; then printf '%s\n' "$f"; cat "$f"; fi
  done | shasum -a 256 | cut -c1-16
}
tree_sha() { # каталоги...
  local d f
  for d in "$@"; do
    [[ -d "$d" ]] || continue
    find "$d" -type f | LC_ALL=C sort | while read -r f; do printf '%s\n' "$f"; cat "$f"; done
  done | shasum -a 256 | cut -c1-16
}
export ANTEC_LITELLM_CONFIG_SHA ANTEC_OPEN_WEBUI_CONFIG_SHA ANTEC_CERBOS_CONFIG_SHA ANTEC_PLATFORM_CONFIG_SHA
compute_config_shas() {
  ANTEC_LITELLM_CONFIG_SHA="$(config_sha "$COMPOSE_DIR/litellm/config.yaml" \
    "$COMPOSE_DIR/common/env-from-files.sh" "$ANTEC_INSTANCE_RENDERED/litellm/models.yaml")"
  ANTEC_OPEN_WEBUI_CONFIG_SHA="$(config_sha "$COMPOSE_DIR/common/env-from-files.sh")"
  # Конфигурация Cerbos и все файлы политик ядра и инсталляции в стабильном порядке.
  ANTEC_CERBOS_CONFIG_SHA="$( { config_sha "$COMPOSE_DIR/cerbos/config.yaml"
    tree_sha "$COMPOSE_DIR/../../policies/cerbos" "$ANTEC_INSTANCE_DIR/policies"; } \
    | shasum -a 256 | cut -c1-16)"
  # env_file не меняет окружение контейнера при compose up без хэша: та же схема.
  ANTEC_PLATFORM_CONFIG_SHA="$(config_sha "$ANTEC_INSTANCE_RENDERED/platform-api.env" \
    "$ANTEC_INSTANCE_RENDERED/platform-api/agents.json" \
    "$ANTEC_INSTANCE_RENDERED/platform-api/mcp_servers.json" \
    "$ANTEC_INSTANCE_RENDERED/platform-api/models.json")"
}
compute_config_shas

compose() {
  local files=(-f "$COMPOSE_DIR/compose.yaml")
  # Сервисы плагинов инсталляции поверх compose ядра (ADR-0008, п. 6).
  if [[ -f "$ANTEC_INSTANCE_DIR/compose.override.yaml" ]]; then
    files+=(-f "$ANTEC_INSTANCE_DIR/compose.override.yaml")
  fi
  docker compose --project-directory "$COMPOSE_DIR" "${files[@]}" "$@"
}

# Сервисы плагинов инсталляции: те, что есть в compose.override.yaml, но не в compose ядра.
instance_services() {
  [[ -f "$ANTEC_INSTANCE_DIR/compose.override.yaml" ]] || return 0
  comm -23 <(compose config --services | LC_ALL=C sort) \
    <(docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yaml" \
      config --services | LC_ALL=C sort)
}

log() { printf '\033[1m[antec]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[antec] ошибка:\033[0m %s\n' "$*" >&2; exit 1; }

# compose.yaml читает производные файлы манифеста (env_file platform-api, модели LiteLLM): без
# них не работает ни одна команда compose, поэтому их нет = рендер сейчас.
if [[ ! -f "$ANTEC_INSTANCE_RENDERED/platform-api.env" \
  || ! -f "$ANTEC_INSTANCE_RENDERED/litellm/models.yaml" \
  || ! -f "$ANTEC_INSTANCE_RENDERED/instance.json" \
  || ! -f "$ANTEC_INSTANCE_RENDERED/platform-api/agents.json" \
  || ! -f "$ANTEC_INSTANCE_RENDERED/platform-api/models.json" ]]; then
  render_instance
fi

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

# После распечатывания узел Raft становится активным не мгновенно: до этого чтения KV могут
# падать. Ждём, пока узел сам станет лидером (sys/leader, без токена).
wait_for_openbao_active() {
  local i
  for i in $(seq 1 30); do
    if compose exec -T openbao bao read -format=json sys/leader 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["data"].get("is_self") else 1)' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  die "OpenBao распечатан, но не стал активным узлом за 30 с; смотрите: docker compose logs openbao"
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

# Секрет KV antec/<путь> как JSON на stdout; `null`, только если OpenBao подтвердил отсутствие
# записи. Любая другая ошибка чтения (доступ, сеть, запечатан) завершает скрипт: отсутствие и
# сбой нельзя путать, иначе up.sh перегенерирует существующие секреты. Нужен BAO_TOKEN.
bao_kv_json() {
  local out err rc=0
  err="$(mktemp)"
  out="$(bao_exec kv get -mount=antec -format=json "$1" 2>"$err")" || rc=$?
  if [[ $rc -eq 0 ]]; then
    printf '%s\n' "$out"
  elif grep -q "^No value found at " "$err"; then
    echo null
  else
    cat "$err" >&2
    rm -f "$err"
    die "не удалось прочитать antec/$1 из OpenBao (код $rc)"
  fi
  rm -f "$err"
}

# Дополняет запись antec/<путь> недостающими случайными ключами; существующие не меняет.
# Аргументы: путь и ключи; ключ вида имя:префикс получает значение с этим префиксом.
# Значения создаются в python3 на хосте и идут в OpenBao только через stdin. Нужен BAO_TOKEN.
ensure_generated_secrets() {
  local path="$1"; shift
  local existing missing
  existing="$(bao_kv_json "$path")"
  missing="$(printf '%s' "$existing" | python3 -c '
import json, sys
doc = json.load(sys.stdin) or {}
have = ((doc.get("data") or {}).get("data") or {})
print(" ".join(spec for spec in sys.argv[1:] if not have.get(spec.split(":", 1)[0])))' "$@")"
  [[ -n "$missing" ]] || return 0
  log "генерация секретов в OpenBao (antec/$path): $(printf '%s' "$missing" | sed 's/:[^ ]*//g')"
  printf '%s' "$existing" | python3 -c '
import json, secrets, sys
doc = json.load(sys.stdin) or {}
data = dict(((doc.get("data") or {}).get("data") or {}))
for spec in sys.argv[1:]:
    name, _, prefix = spec.partition(":")
    data[name] = prefix + secrets.token_urlsafe(32)
print(json.dumps(data))' $missing \
    | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec "$path" - >/dev/null
}

# Копирует поле одной записи KV в другую (значение идёт только через stdin). Запись-приёмник
# создаётся, если её нет (kv patch работает только с существующей записью).
copy_secret_field() { # путь-источник поле-источник путь-приёмник поле-приёмник
  local verb=patch
  [[ "$(bao_kv_json "$3")" == null ]] && verb=put
  bao_exec kv get -mount=antec -field="$2" "$1" \
    | compose exec -T -e BAO_TOKEN openbao bao kv "$verb" -mount=antec "$3" "$4=-" >/dev/null
}

# Копирует пароль роли PostgreSQL из antec/postgres в antec/<путь>:db_password.
copy_db_password() { # роль путь
  copy_secret_field postgres "$1" "$2" db_password
}

# Входные данные scripts/keycloak.py (JSON на stdout): секреты antec/keycloak и, если задан,
# antec/entra (tenant_id, client_id; client_secret не передаётся, он в vault-файле Keycloak).
# Нужен BAO_TOKEN с правом чтения обоих путей.
keycloak_inputs() {
  # Оба документа идут через stdin одного python3, а не через argv. Ошибка чтения любого
  # из них завершает конвейер (pipefail), а не превращается в «Entra не настроен».
  { bao_kv_json keycloak && bao_kv_json entra; } | python3 -c '
import json, sys
text, dec, docs, pos = sys.stdin.read(), json.JSONDecoder(), [], 0
if not text.strip():
    sys.exit(1)  # чтение из OpenBao не удалось, причина уже напечатана bao_kv_json
while len(docs) < 2:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    doc, pos = dec.raw_decode(text, pos)
    docs.append(doc)
if not docs[0]:
    sys.exit("в OpenBao нет antec/keycloak; запустите scripts/up.sh")
kc = docs[0]["data"]["data"]
entra = docs[1]["data"]["data"] if docs[1] else None
kc["entra"] = {k: entra[k] for k in ("tenant_id", "client_id")} if entra else None
# Аудитории MCP-серверов инсталляции: клиенты Keycloak для обмена токена (ADR-0010).
kc["mcp_audiences"] = json.load(open(sys.argv[1])).get("mcp_audiences", [])
print(json.dumps(kc))' "$ANTEC_INSTANCE_RENDERED/instance.json"
}

# Входные данные scripts/litellm_provision.py (JSON на stdout): мастер-ключ LiteLLM и
# виртуальный ключ Platform API. Оба документа идут через stdin одного python3. Нужен BAO_TOKEN.
litellm_provision_inputs() {
  { bao_kv_json litellm && bao_kv_json platform-api; } | python3 -c '
import json, sys
models = json.loads(sys.argv[1])
text, dec, docs, pos = sys.stdin.read(), json.JSONDecoder(), [], 0
if not text.strip():
    sys.exit(1)  # чтение из OpenBao не удалось, причина уже напечатана bao_kv_json
while len(docs) < 2:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    doc, pos = dec.raw_decode(text, pos)
    docs.append(doc)
if not docs[0] or not docs[1]:
    sys.exit("в OpenBao нет antec/litellm или antec/platform-api; запустите scripts/up.sh")
print(json.dumps({"master_key": docs[0]["data"]["data"]["master_key"],
                  "platform_api_key": docs[1]["data"]["data"]["litellm_key"],
                  "models": models}))' "$(instance_models_json)"
}

# Роль сервиса Platform API (ADR-0006, п. 9; ADR-0012, п. 2): создаётся, если её нет, пароль
# приводится к OpenBao. Пароль читается psql из файла секретов внутри контейнера postgres (не
# argv, не окружение). Права на таблицы выдают миграции (владелец схемы platform).
ensure_platform_role() { # роль лимит_соединений
  { printf '\\set role %s\n\\set lim %s\n\\set pw `cat /run/antec-secrets/%s`\n' "$1" "$2" "$1"
    cat <<'SQL'
select format('create role %I login', :'role')
  where not exists (select 1 from pg_roles where rolname = :'role') \gexec
alter role :"role" login connection limit :lim password :'pw';
grant connect on database platform to :"role";
\connect platform
grant usage on schema public to :"role";
SQL
  } | pg_exec psql -X -q -v ON_ERROR_STOP=1 -d postgres >/dev/null
}

# Сверка аккаунтов Open WebUI с Keycloak (open-webui/sync-identities.sql, ADR-0018): перенос
# аккаунтов на antec_uid, аккаунт breakglass в пустом Open WebUI, снятие роли admin у тех, у кого
# её нет в Keycloak. Значения в psql -v несекретные: antec_uid, id и bcrypt одноразового пароля.
json_part() { # поле: JSON-значение поля объекта со stdin
  python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$1"
}

sync_open_webui_identities() {
  local ids seed_id seed_hash
  ids="$(keycloak_inputs | python3 "$COMPOSE_DIR/scripts/keycloak.py" open-webui-identities)"
  seed_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  seed_hash="$(compose exec -T open-webui python -c \
    'import bcrypt, secrets; print(bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode())')"
  compose exec -T -u postgres postgres psql -X -q -At -d openwebui \
      -v admins="$(json_part admins <<<"$ids")" -v uid_map="$(json_part uid_by_id <<<"$ids")" \
      -v seed_id="$seed_id" -v seed_hash="$seed_hash" -v now="$(date +%s)" \
    < "$COMPOSE_DIR/open-webui/sync-identities.sql" \
    | sed 's/^/  open-webui: /' >&2
}
