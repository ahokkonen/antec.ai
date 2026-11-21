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

# Значение поля во временный файл; пустое значение = ошибка. Итоговые файлы заменяются только
# после успешного чтения всех значений цели, поэтому сбой не оставляет файлы с владельцем root.
fetch() { # путь поле файл
  bao kv get -mount=antec -field="$2" "$1" > "$3"
  if [ ! -s "$3" ]; then
    echo "render: пустое значение antec/$1:$2" >&2
    exit 1
  fi
}

case "$target" in
  postgres)
    out=/out/postgres
    keys="superuser keycloak litellm openwebui platform dbos"
    trap 'rm -f "$out"/.*.new' EXIT
    for key in $keys; do
      fetch postgres "$key" "$out/.$key.new"
    done
    for key in $keys; do
      mv -f "$out/.$key.new" "$out/$key"
    done
    # Владелец: пользователь postgres в официальном образе (uid/gid 999).
    owner=999:999
    ;;
  keycloak)
    out=/out/keycloak
    mkdir -p "$out/vault"
    # Сначала все чтения во временные файлы, потом замена итоговых: при любой ошибке чтения
    # прежние keycloak.conf и vault-файл остаются нетронутыми (с прежними владельцем и правами).
    trap 'rm -f "$out/.db" "$out/.bootstrap" "$out/keycloak.conf.tmp" "$out/vault/.entra.tmp" "$out/vault/.entra.err"' EXIT
    fetch keycloak db_password "$out/.db"
    fetch keycloak bootstrap_client_secret "$out/.bootstrap"
    # Секрет Entra необязателен: без него realm работает, но вход через Entra недоступен
    # (scripts/set-entra.sh). Отсутствие записи и ошибка чтения различаются.
    tmp="$out/vault/.entra.tmp"; err="$out/vault/.entra.err"
    if bao kv get -mount=antec -field=client_secret entra > "$tmp" 2> "$err" && [ -s "$tmp" ]; then
      entra=present
    elif grep -q "^No value found at " "$err"; then
      entra=absent
    else
      cat "$err" >&2
      echo "render: не удалось прочитать antec/entra; прежние секреты Keycloak сохранены" >&2
      exit 1
    fi
    # keycloak.conf: только секретные опции; остальное в command сервиса (compose.yaml).
    # Значения сгенерированы как token_urlsafe и не требуют экранирования.
    {
      printf 'db-password=%s\n' "$(cat "$out/.db")"
      printf 'bootstrap-admin-client-id=antec-bootstrap\n'
      printf 'bootstrap-admin-client-secret=%s\n' "$(cat "$out/.bootstrap")"
    } > "$out/keycloak.conf.tmp"
    mv -f "$out/keycloak.conf.tmp" "$out/keycloak.conf"
    # Файловый vault Keycloak: <realm>_<ключ>.
    if [ "$entra" = present ]; then
      mv -f "$tmp" "$out/vault/antec_entra-client-secret"
    else
      rm -f "$out/vault/antec_entra-client-secret"
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
