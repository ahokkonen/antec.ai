#!/usr/bin/env bash
# Проверка восстановления: разворачивает бэкап в одноразовых изолированных контейнерах
# (--network none) и сверяет с манифестом: контрольные суммы файлов, состав и число строк
# таблиц, канарейку, расширение vector; Raft-снапшот OpenBao восстанавливается и
# распечатывается ключом установки из манифеста; ролям назначаются пароли из восстановленного
# OpenBao, и каждая роль входит в свою БД по SCRAM. Рабочий стенд не затрагивается.
# Использование: restore-verify.sh [каталог бэкапа]  (по умолчанию последний)
set -euo pipefail
source "$(dirname "$0")/lib.sh"

dir="${1:-}"
if [[ -z "$dir" ]]; then
  dir="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
fi
[[ -f "$dir/manifest.json" ]] || die "не найден манифест в ${dir:-$BACKUP_ROOT}"
log "проверка бэкапа $dir"

python3 - "$dir" <<'PY' || die "контрольные суммы не совпадают"
import hashlib, json, pathlib, sys
d = pathlib.Path(sys.argv[1])
m = json.loads((d / "manifest.json").read_text())
bad = [n for n, h in m["files"].items() if hashlib.sha256((d / n).read_bytes()).hexdigest() != h]
if bad:
    print("повреждены:", ", ".join(bad), file=sys.stderr)
    sys.exit(1)
PY
log "  контрольные суммы файлов: ок"

name="antec-restore-verify-$$"
bao_name="antec-restore-verify-bao-$$"
cleanup() { docker rm -f "$name" "$bao_name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# Изолированный одноразовый кластер: без сети. Те же правила аутентификации, что на стенде:
# SCRAM для TCP, peer для сокета. Пароль суперпользователя случайный и нигде не хранится;
# передаётся через окружение docker CLI, не через argv.
POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export POSTGRES_PASSWORD
docker run -d --name "$name" --network none \
  -e POSTGRES_PASSWORD \
  -e POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256 --auth-local=peer" \
  --security-opt no-new-privileges:true \
  "$POSTGRES_IMAGE" >/dev/null
unset POSTGRES_PASSWORD
rx() { docker exec -i -u postgres "$name" "$@"; }

for _ in $(seq 1 60); do
  # Entrypoint перезапускает сервер после initdb: ждём финальный запуск.
  if rx pg_isready -q 2>/dev/null && docker logs "$name" 2>&1 | grep -q "PostgreSQL init process complete"; then
    break
  fi
  sleep 1
done
rx pg_isready -q || die "временный кластер не запустился"

# Роли (без паролей: бэкап снят с --no-role-passwords). Роль postgres уже существует.
rx psql -q -d postgres < "$dir/globals.sql" 2>&1 | grep -v 'role "postgres" already exists' | grep -i error \
  && die "ошибки при восстановлении ролей" || true

for db in "${DATABASES[@]}"; do
  rx pg_restore --create --exit-on-error --dbname=postgres < "$dir/$db.dump"
done
log "  pg_restore: ок (${DATABASES[*]})"

actual="{"
for db in "${DATABASES[@]}"; do
  actual+="\"$db\": $(rx psql -At -d "$db" -c "$ROWCOUNT_SQL"),"
done
actual="${actual%,}}"
canary_found="$(rx psql -At -d platform -c "select count(*) from antec_ops.backup_canary where token = '$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["canary"])' "$dir/manifest.json")'")"
vector="$(rx psql -At -d platform -c "select extversion from pg_extension where extname = 'vector'")"

python3 - "$dir" "$actual" "$canary_found" "$vector" <<'PY' || die "восстановленные данные не совпадают с манифестом"
import json, pathlib, sys
d, actual, canary_found, vector = sys.argv[1:]
m = json.loads((pathlib.Path(d) / "manifest.json").read_text())
expected, actual = m["row_counts"], json.loads(actual)
problems = []
for db, tables in expected.items():
    got = actual.get(db)
    if got != tables:
        problems.append(f"{db}: ожидалось {tables}, получено {got}")
if canary_found != "1":
    problems.append("канарейка не найдена в platform.antec_ops.backup_canary")
if not vector:
    problems.append("расширение vector отсутствует в platform")
for p in problems:
    print(p, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
log "  таблицы и число строк: ок; канарейка: ок; vector $vector: ок"

# --- OpenBao: восстановление Raft-снапшота в одноразовый узел ---
require_macos_keychain
# Ключи той установки, которая сделала бэкап (manifest.json), а не текущей.
installation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("openbao_installation", ""))' "$dir/manifest.json")"
[[ -n "$installation" ]] || installation="$(current_installation)"
unseal_key_name="$(kc_unseal_name "$installation")"
root_token_name="$(kc_root_name "$installation")"
[[ -n "$(kc_get "$unseal_key_name")" ]] \
  || die "в Связке ключей нет ключа установки $installation: снапшот OpenBao этого бэкапа не восстановить"
bao_image="$(compose config --images | grep openbao | head -1)"
docker run -d --name "$bao_name" --network none \
  --security-opt no-new-privileges:true \
  -v "$COMPOSE_DIR/openbao/config.hcl:/openbao/config/config.hcl:ro" \
  "$bao_image" server >/dev/null
bx() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 "$bao_name" "$@"; }
for _ in $(seq 1 30); do
  rc=0; bx bao status >/dev/null 2>&1 || rc=$?
  [[ "$rc" == "2" ]] && break
  sleep 1
done
# Временный узел инициализируется своими ключами только чтобы принять снапшот.
tmp_init="$(bx bao operator init -key-shares=1 -key-threshold=1 -format=json)"
printf '%s' "$tmp_init" | python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_keys_b64"][0], end="")' \
  | bx bao write sys/unseal key=- >/dev/null
BAO_TOKEN="$(printf '%s' "$tmp_init" | json_field root_token)"; export BAO_TOKEN
unset tmp_init
for _ in $(seq 1 30); do bx bao status >/dev/null 2>&1 && break; sleep 1; done
# Через stdin от пользователя openbao: файл сразу с нужным владельцем и правами 600.
docker exec -i -u openbao "$bao_name" sh -c 'umask 077; cat > /tmp/openbao.snap' < "$dir/openbao.snap"
docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN "$bao_name" \
  bao operator raft snapshot restore -force /tmp/openbao.snap
# После восстановления действуют ключи и токены из снапшота.
docker restart "$bao_name" >/dev/null
for _ in $(seq 1 30); do
  rc=0; bx bao status >/dev/null 2>&1 || rc=$?
  [[ "$rc" == "2" ]] && break
  sleep 1
done
kc_get "$unseal_key_name" | tr -d '\n' | bx bao write sys/unseal key=- >/dev/null \
  || die "снапшот OpenBao не распечатывается исходным ключом"
BAO_TOKEN="$(kc_get "$root_token_name")"; export BAO_TOKEN
secrets_json="$(docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN "$bao_name" \
  bao kv get -mount=antec -format=json postgres)"
# Секреты сервисов в восстановленном OpenBao. В бэкапах до появления сервиса их нет; это
# допустимо только при пустой БД сервиса в бэкапе: иначе восстановленные данные недоступны
# (Keycloak: bootstrap-клиент удалён; LiteLLM: без salt key не расшифровать учётные данные в БД).
# Печатает «ok», «нет» или «неполные: ...».
service_secrets() { # путь ключ...
  local path="$1"; shift
  docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN "$bao_name" \
    bao kv get -mount=antec -format=json "$path" 2>/dev/null \
    | python3 -c '
import json, sys
need = set(sys.argv[1:])
have = {k for k, v in json.load(sys.stdin)["data"]["data"].items() if v}
print("ok" if need <= have else "неполные: нет " + ", ".join(sorted(need - have)))' "$@" 2>/dev/null \
    || echo "нет"
}
kc_keys="$(service_secrets keycloak db_password bootstrap_client_secret automation_client_secret \
  admin_password breakglass_password platform_api_client_secret open_webui_client_secret)"
llm_keys="$(service_secrets litellm db_password master_key salt_key)"
owui_keys="$(service_secrets open-webui db_password webui_secret_key oauth_client_secret)"
papi_keys="$(service_secrets platform-api db_password litellm_key)"
pmig_keys="$(service_secrets platform-migrate db_password)"
# Аудит (ADR-0012, п. 4): подписи точек ключом из восстановленного OpenBao, цепочка в
# восстановленной БД. В бэкапах до ADR-0012 точек нет.
points="$dir/audit-checkpoints.jsonl"
if [[ ! -s "$points" && "$(rx psql -X -At -d platform \
    -c "select to_regclass('audit.chain_head') is not null")" == t ]]; then
  die "в бэкапе есть схема audit, но нет контрольных точек ($points)"
fi
if [[ -s "$points" ]]; then
  mark="$(docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN "$bao_name" \
    bao kv get -mount=antec -field=last "$AUDIT_KEY")" || die "в снапшоте OpenBao нет отметки точек аудита"
  audit_chain last "$points" "$mark" >/dev/null || die "файл точек аудита в бэкапе неполный"
  audit_verify_signatures "$points" docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN \
    "$bao_name" bao || die "подписи контрольных точек аудита не подтверждены"
  audit_state="$(rx psql -X -q -At -d platform < "$COMPOSE_DIR/postgres/audit-export.sql" \
    | audit_chain verify "$points")" || die "аудит в бэкапе не прошёл проверку"
  log "  аудит: цепочка и точки подтверждены, событий $(json_field count <<<"$audit_state")"
fi
pwrk_keys="$(service_secrets platform-worker db_password)"
psub_keys="$(service_secrets platform-subject db_password)"
unset BAO_TOKEN
# Роли PostgreSQL и их БД: роль на каждую БД и роль сервиса Platform API (ADR-0006, п. 9).
# Бэкапы старых установок не содержат ролей platform_app (до ADR-0006), platform_worker и
# platform_subject (до ADR-0012): роль проверяется, если есть в OpenBao.
ROLES=("${DATABASES[@]}")
role_db() { if [[ "$1" == platform_app || "$1" == platform_worker || "$1" == platform_subject ]]; then echo platform; else echo "$1"; fi; }
for role in platform_app platform_worker platform_subject; do
  if printf '%s' "$secrets_json" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["data"]["data"].get(sys.argv[1]) else 1)' "$role"; then
    ROLES+=("$role")
  fi
done
keys="$(printf '%s' "$secrets_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]["data"]
need = ["superuser", *sys.argv[1:]]
missing = [k for k in need if not data.get(k)]
print(len(need) - len(missing))
if missing:
    sys.exit("нет: " + ", ".join(missing))' "${ROLES[@]}")" || die "в восстановленном OpenBao неполные секреты antec/postgres: $keys"
# Строки данных сервиса в БД. В platform служебная схема antec_ops (канарейка бэкапа) есть до
# появления Platform API, поэтому считаются только таблицы сервиса (схема public).
db_rows() { # БД
  python3 -c '
import json, sys
counts = json.load(open(sys.argv[1]))["row_counts"].get(sys.argv[2]) or {}
skip = ("antec_ops.",) if sys.argv[2] == "platform" else ()
print(sum(v for k, v in counts.items() if not k.startswith(skip)))' "$dir/manifest.json" "$1"
}
for entry in "keycloak:keycloak:$kc_keys" "litellm:litellm:$llm_keys" "open-webui:openwebui:$owui_keys" \
    "platform-api:platform:$papi_keys" "platform-migrate:platform:$pmig_keys"; do
  path="${entry%%:*}"; rest="${entry#*:}"; db="${rest%%:*}"; state="${rest#*:}"
  rows="$(db_rows "$db")"
  if [[ "$state" == "нет" && "$rows" != "0" ]]; then
    die "в бэкапе БД $db не пуста ($rows строк), а в OpenBao нет antec/$path"
  fi
  [[ "$state" == "ok" || "$state" == "нет" ]] || die "в восстановленном OpenBao antec/$path $state"
done
for state in "$pwrk_keys" "$psub_keys"; do
  [[ "$state" == "ok" || "$state" == "нет" ]] \
    || die "в восстановленном OpenBao antec/platform-worker или antec/platform-subject $state"
done
log "  OpenBao: снапшот восстановлен, распечатан исходным ключом установки $installation, antec/postgres: $keys ключей, antec/keycloak: $kc_keys, antec/litellm: $llm_keys, antec/open-webui: $owui_keys, antec/platform-api: $papi_keys, antec/platform-migrate: $pmig_keys, antec/platform-worker: $pwrk_keys, antec/platform-subject: $psub_keys"

# --- Роли: пароли из восстановленного OpenBao и вход каждой роли в свою БД по SCRAM ---
# Бэкап ролей снят без паролей; источник истины паролей OpenBao. SQL с паролями идёт через stdin.
printf '%s' "$secrets_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]["data"]
for role in sys.argv[1:]:
    pw = data[role].replace("\x27", "\x27\x27")
    print(f"ALTER ROLE \"{role}\" PASSWORD \x27{pw}\x27;")
' "${ROLES[@]}" | rx psql -q -v ON_ERROR_STOP=1 -d postgres >/dev/null
for role in "${ROLES[@]}"; do
  db="$(role_db "$role")"
  PGPASSWORD="$(printf '%s' "$secrets_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["data"][sys.argv[1]])' "$role")"
  export PGPASSWORD
  who="$(docker exec -i -e PGPASSWORD "$name" psql -h 127.0.0.1 -U "$role" -d "$db" -At -c 'select system_user' 2>&1)" \
    || die "роль $role не входит в восстановленную БД $db: $who"
  unset PGPASSWORD
  [[ "$who" == "scram-sha-256:$role" ]] || die "роль $role вошла не по SCRAM: $who"
done
unset secrets_json
log "  роли: пароли из OpenBao назначены, вход по SCRAM в свою БД: ок (${ROLES[*]})"
log "восстановление проверено: $dir"
