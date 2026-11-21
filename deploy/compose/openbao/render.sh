#!/bin/sh
# Одноразовая выгрузка секретов из OpenBao в том postgres-secrets (ADR-0017).
# Токен приходит через stdin (`compose run -T`), живёт 5 минут и умеет только читать
# antec/postgres. Значения не печатаются и не попадают в argv и окружение контейнера.
set -eu

out=/out
IFS= read -r BAO_TOKEN || true
if [ -z "${BAO_TOKEN:-}" ]; then
  echo "render: нет токена на stdin; запускайте стенд через scripts/up.sh" >&2
  exit 1
fi
export BAO_TOKEN

umask 077
for key in superuser keycloak litellm openwebui platform dbos; do
  tmp="$out/.$key.tmp"
  bao kv get -mount=antec -field="$key" postgres > "$tmp"
  if [ ! -s "$tmp" ]; then
    echo "render: пустое значение antec/postgres:$key" >&2
    exit 1
  fi
  mv -f "$tmp" "$out/$key"
done
# Владелец: пользователь postgres в официальном образе (uid/gid 999).
chown 999:999 "$out"/*
chmod 0400 "$out"/*
chmod 0700 "$out"
chown 999:999 "$out"
echo "render: секреты postgres выгружены"
