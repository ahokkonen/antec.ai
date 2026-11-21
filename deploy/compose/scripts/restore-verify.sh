#!/usr/bin/env bash
# Проверка восстановления: разворачивает бэкап в одноразовых изолированных контейнерах
# (--network none) и сверяет с манифестом: контрольные суммы файлов, состав и число строк
# таблиц, канарейку, расширение vector; Raft-снапшот OpenBao восстанавливается и
# распечатывается исходным ключом, секреты antec/postgres читаются. Рабочий стенд не затрагивается.
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
# Изолированный одноразовый кластер: без сети, вход только через локальный сокет.
docker run -d --name "$name" --network none \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  --security-opt no-new-privileges:true \
  "$POSTGRES_IMAGE" >/dev/null
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
kc_get "$KC_UNSEAL" | tr -d '\n' | bx bao write sys/unseal key=- >/dev/null \
  || die "снапшот OpenBao не распечатывается исходным ключом"
BAO_TOKEN="$(kc_get "$KC_ROOT")"; export BAO_TOKEN
keys="$(docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN "$bao_name" \
  bao kv get -mount=antec -format=json postgres \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]["data"]))')"
unset BAO_TOKEN
[[ "$keys" == "6" ]] || die "в восстановленном OpenBao неполные секреты antec/postgres ($keys из 6)"
log "  OpenBao: снапшот восстановлен, распечатан исходным ключом, antec/postgres: $keys ключей"
log "восстановление проверено: $dir"
