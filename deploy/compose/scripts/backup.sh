#!/usr/bin/env bash
# Бэкап локального стенда: pg_dump каждой БД, глобальные объекты (роли), Raft-снапшот OpenBao.
# Каталог вне репозитория, права 700. Содержимое чувствительное (хэши паролей ролей, данные).
# Это логический бэкап; непрерывный WAL-архив (PITR) решается отдельным ADR.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

ts="$(date -u +%Y%m%dT%H%M%SZ)"
dir="$BACKUP_ROOT/$ts"
umask 077
mkdir -p "$dir"
log "бэкап в $dir"

# Канарейка: случайная запись, которую restore-verify обязан найти после восстановления.
canary="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
pg_exec psql -v ON_ERROR_STOP=1 -q -d platform -v canary="$canary" <<'SQL'
CREATE SCHEMA IF NOT EXISTS antec_ops AUTHORIZATION postgres;
CREATE TABLE IF NOT EXISTS antec_ops.backup_canary (
  taken_at timestamptz PRIMARY KEY DEFAULT now(),
  token    text NOT NULL
);
INSERT INTO antec_ops.backup_canary (token) VALUES (:'canary');
SQL

pg_exec pg_dumpall --globals-only --no-role-passwords > "$dir/globals.sql"
counts="{"
for db in "${DATABASES[@]}"; do
  rows="$(pg_exec psql -At -d "$db" -c "$ROWCOUNT_SQL")"
  pg_exec pg_dump --format=custom --compress=6 --dbname="$db" > "$dir/$db.dump"
  counts+="\"$db\": $rows,"
  log "  $db: $(du -h "$dir/$db.dump" | cut -f1)"
done
counts="${counts%,}}"

BAO_TOKEN="$(kc_get "$KC_ROOT")"; export BAO_TOKEN
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
compose exec -T -e BAO_TOKEN openbao sh -c \
  'umask 077; f="$(mktemp)"; bao operator raft snapshot save "$f" >/dev/null && cat "$f"; rm -f "$f"' \
  > "$dir/openbao.snap"
unset BAO_TOKEN
log "  openbao: $(du -h "$dir/openbao.snap" | cut -f1)"

pg_version="$(pg_exec psql -At -c 'show server_version')"
python3 - "$dir" "$ts" "$canary" "$pg_version" "$counts" <<'PY'
import hashlib, json, pathlib, sys
d, ts, canary, pg_version, counts = sys.argv[1:]
files = sorted(p for p in pathlib.Path(d).iterdir() if p.name != "manifest.json")
manifest = {
    "created_at": ts,
    "postgres_version": pg_version,
    "canary": canary,
    "row_counts": json.loads(counts),
    "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
}
(pathlib.Path(d) / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
PY
log "готово: $dir"
echo "$dir"
