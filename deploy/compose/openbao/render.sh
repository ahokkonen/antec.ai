#!/bin/sh
# Одноразовая выгрузка секретов из OpenBao в том сервиса (ADR-0017, ADR-0007).
#   render.sh postgres   пароли ролей PostgreSQL в /out/postgres (uid 999)
#   render.sh keycloak   keycloak.conf и vault-файлы realm в /out/keycloak (uid 1000)
# Токен приходит через stdin (`compose run -T`), живёт 5 минут и умеет читать только
# пути своей цели. Значения не печатаются и не попадают в argv и окружение контейнера.
set -eu

target="${1:-}"
IFS= read -r BAO_TOKEN || true
if [ -z "${BAO_TOKEN:-}" ]; then
  echo "render: нет токена на stdin; запускайте стенд через scripts/up.sh" >&2
  exit 1
fi
export BAO_TOKEN
umask 077

# Значение поля в файл через временный файл; пустое значение = ошибка.
fetch() { # путь поле файл
  bao kv get -mount=antec -field="$2" "$1" > "$3.tmp"
  if [ ! -s "$3.tmp" ]; then
    echo "render: пустое значение antec/$1:$2" >&2
    rm -f "$3.tmp"
    exit 1
  fi
  mv -f "$3.tmp" "$3"
}

case "$target" in
  postgres)
    out=/out/postgres
    for key in superuser keycloak litellm openwebui platform dbos; do
      fetch postgres "$key" "$out/$key"
    done
    # Владелец: пользователь postgres в официальном образе (uid/gid 999).
    owner=999:999
    ;;
  keycloak)
    out=/out/keycloak
    mkdir -p "$out/vault"
    fetch keycloak db_password "$out/.db"
    fetch keycloak bootstrap_client_secret "$out/.bootstrap"
    # keycloak.conf: только секретные опции; остальное в command сервиса (compose.yaml).
    # Значения сгенерированы как token_urlsafe и не требуют экранирования.
    {
      printf 'db-password=%s\n' "$(cat "$out/.db")"
      printf 'bootstrap-admin-client-id=antec-bootstrap\n'
      printf 'bootstrap-admin-client-secret=%s\n' "$(cat "$out/.bootstrap")"
    } > "$out/keycloak.conf.tmp"
    mv -f "$out/keycloak.conf.tmp" "$out/keycloak.conf"
    rm -f "$out/.db" "$out/.bootstrap"
    # Файловый vault Keycloak: <realm>_<ключ>. Секрет Entra необязателен: без него realm
    # работает, но вход через Entra недоступен (scripts/set-entra.sh).
    if bao kv get -mount=antec -field=client_secret entra > "$out/vault/.entra.tmp" 2>/dev/null \
        && [ -s "$out/vault/.entra.tmp" ]; then
      mv -f "$out/vault/.entra.tmp" "$out/vault/antec_entra-client-secret"
    else
      rm -f "$out/vault/.entra.tmp" "$out/vault/antec_entra-client-secret"
      echo "render: секрет Entra не задан (antec/entra), вход через Entra будет недоступен" >&2
    fi
    # Пользователь keycloak в официальном образе: uid 1000, группа root (gid 0).
    owner=1000:0
    ;;
  *)
    echo "render: использование: render.sh postgres|keycloak" >&2
    exit 2
    ;;
esac

chown -R "$owner" "$out"
find "$out" -type f -exec chmod 0400 {} +
find "$out" -type d -exec chmod 0700 {} +
echo "render: секреты $target выгружены"
