#!/bin/sh
# Одноразовая выгрузка секретов из OpenBao в том сервиса (ADR-0017, ADR-0007).
#   render.sh postgres   пароли ролей PostgreSQL в /out/postgres (uid 999)
#   render.sh keycloak   keycloak.conf и vault-файлы realm в /out/keycloak (uid 1000)
#   render.sh litellm    файлы-переменные окружения LiteLLM в /out/litellm (uid 65534)
#   render.sh open-webui файлы-переменные окружения Open WebUI в /out/open-webui (uid 65534)
#   render.sh platform-api      секреты Platform API в /out/platform-api (uid 65534)
#   render.sh platform-migrate  пароль владельца схемы platform в /out/platform-migrate (uid 65534)
#   render.sh platform-worker   пароль роли platform_worker в /out/platform-worker (uid 65534)
#   render.sh platform-subject  пароль роли platform_subject в /out/platform-subject (uid 65534)
#   render.sh grafana   секреты и SMTP Grafana в /out/grafana (uid 472, ADR-0013)
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

# Замена итогового файла с учётом изменений: up.sh перезапускает работающий сервис, только
# если его секреты изменились (на stdout печатается «changed», остальной вывод в stderr).
changed=0
replace() { # новый итоговый
  if [ ! -f "$2" ] || ! cmp -s "$1" "$2"; then changed=1; fi
  mv -f "$1" "$2"
}
remove() { # итоговый
  if [ -e "$1" ]; then changed=1; fi
  rm -f "$1"
}

case "$target" in
  postgres)
    out=/out/postgres
    keys="superuser keycloak litellm openwebui platform dbos platform_app platform_worker platform_subject platform_metrics"
    trap 'rm -f "$out"/.*.new' EXIT
    for key in $keys; do
      fetch postgres "$key" "$out/.$key.new"
    done
    for key in $keys; do
      replace "$out/.$key.new" "$out/$key"
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
    replace "$out/keycloak.conf.tmp" "$out/keycloak.conf"
    # Файловый vault Keycloak: <realm>_<ключ>.
    if [ "$entra" = present ]; then
      replace "$tmp" "$out/vault/antec_entra-client-secret"
    else
      remove "$out/vault/antec_entra-client-secret"
      echo "render: секрет Entra не задан (antec/entra), вход через Entra будет недоступен" >&2
    fi
    # Пользователь keycloak в официальном образе: uid 1000, группа root (gid 0).
    owner=1000:0
    ;;
  litellm)
    # Имя файла = имя переменной окружения; common/env-from-files.sh переносит их в окружение
    # процесса LiteLLM (ADR-0018). Сначала все чтения, потом замена итоговых файлов.
    out=/out/litellm
    trap 'rm -f "$out"/.*.new "$out"/.*.err' EXIT
    fetch litellm master_key "$out/.LITELLM_MASTER_KEY.new"
    fetch litellm salt_key "$out/.LITELLM_SALT_KEY.new"
    fetch litellm db_password "$out/.db.new"
    # Пароль сгенерирован как token_urlsafe: в URL экранирование не нужно.
    printf 'postgresql://litellm:%s@postgres:5432/litellm' "$(cat "$out/.db.new")" > "$out/.DATABASE_URL.new"
    rm -f "$out/.db.new"
    # Ключи провайдеров необязательны (scripts/set-llm-key.sh): отсутствие записи или поля
    # означает «провайдер не настроен», любая другая ошибка чтения прерывает выгрузку.
    providers=""
    for pair in anthropic_api_key:ANTHROPIC_API_KEY openai_api_key:OPENAI_API_KEY openrouter_api_key:OPENROUTER_API_KEY; do
      field="${pair%%:*}"; var="${pair##*:}"
      if bao kv get -mount=antec -field="$field" llm-providers > "$out/.$var.new" 2> "$out/.$var.err" \
          && [ -s "$out/.$var.new" ]; then
        providers="$providers $var"
      elif grep -q -e "^No value found at " -e "not present in" "$out/.$var.err"; then
        rm -f "$out/.$var.new"
      else
        cat "$out/.$var.err" >&2
        echo "render: не удалось прочитать antec/llm-providers:$field; прежние секреты LiteLLM сохранены" >&2
        exit 1
      fi
    done
    for var in LITELLM_MASTER_KEY LITELLM_SALT_KEY DATABASE_URL; do
      replace "$out/.$var.new" "$out/$var"
    done
    for var in ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY; do
      case " $providers " in
        *" $var "*) replace "$out/.$var.new" "$out/$var" ;;
        *) remove "$out/$var" ;;
      esac
    done
    [ -n "$providers" ] || echo "render: ключи провайдеров не заданы (scripts/set-llm-key.sh): модели недоступны" >&2
    # Пользователь образа litellm-non_root: nobody (uid/gid 65534).
    owner=65534:65534
    ;;
  open-webui)
    # Файлы-переменные окружения Open WebUI (common/env-from-files.sh, ADR-0018).
    # Несекретные параметры подключения к БД заданы в compose.yaml; здесь только пароль.
    out=/out/open-webui
    trap 'rm -f "$out"/.*.new' EXIT
    fetch open-webui webui_secret_key "$out/.WEBUI_SECRET_KEY.new"
    fetch open-webui db_password "$out/.DATABASE_PASSWORD.new"
    fetch open-webui oauth_client_secret "$out/.OAUTH_CLIENT_SECRET.new"
    for var in WEBUI_SECRET_KEY DATABASE_PASSWORD OAUTH_CLIENT_SECRET; do
      replace "$out/.$var.new" "$out/$var"
    done
    # Ключ LiteLLM у Open WebUI больше не нужен: чат идёт через Platform API (ADR-0006).
    remove "$out/OPENAI_API_KEYS"
    owner=65534:65534
    ;;
  platform-api)
    # Файлы секретов pydantic-settings: имя = имя переменной в нижнем регистре (ADR-0006, п. 10).
    out=/out/platform-api
    trap 'rm -f "$out"/.*.new' EXIT
    fetch platform-api db_password "$out/.platform_database_password.new"
    fetch platform-api litellm_key "$out/.platform_gateway_api_key.new"
    # Секрет клиента platform-api в Keycloak: обмен токена для MCP-серверов (ADR-0010, п. 6).
    fetch platform-api oidc_client_secret "$out/.platform_oidc_client_secret.new"
    for var in platform_database_password platform_gateway_api_key platform_oidc_client_secret; do
      replace "$out/.$var.new" "$out/$var"
    done
    owner=65534:65534
    ;;
  platform-migrate|platform-worker|platform-subject)
    out=/out/$target
    trap 'rm -f "$out"/.*.new' EXIT
    fetch "$target" db_password "$out/.platform_database_password.new"
    replace "$out/.platform_database_password.new" "$out/platform_database_password"
    owner=65534:65534
    ;;
  grafana)
    # Файлы для GF_*__FILE (ADR-0013): секрет клиента grafana, пароль администратора (вход
    # формой выключен), пароль роли platform_metrics для источника данных PostgreSQL, SMTP.
    out=/out/grafana
    trap 'rm -f "$out"/.*.new "$out"/.smtp.err' EXIT
    fetch grafana oauth_client_secret "$out/.oauth_client_secret.new"
    fetch grafana admin_password "$out/.admin_password.new"
    fetch grafana db_password "$out/.db_password.new"
    # SMTP необязателен (scripts/set-smtp.sh): без записи алерты видны только в Grafana.
    # Отсутствие записи и ошибка чтения различаются.
    if bao kv get -mount=antec -field=host smtp > "$out/.smtp_host.new" 2> "$out/.smtp.err" \
        && [ -s "$out/.smtp_host.new" ]; then
      fetch smtp user "$out/.smtp_user.new"
      fetch smtp password "$out/.smtp_password.new"
      fetch smtp from_address "$out/.smtp_from_address.new"
      printf 'true' > "$out/.smtp_enabled.new"
    elif grep -q "^No value found at " "$out/.smtp.err"; then
      printf 'false' > "$out/.smtp_enabled.new"
      # Grafana читает все GF_SMTP_*__FILE: при выключенном SMTP файлы есть, но пусты по смыслу.
      # Grafana проверяет адрес отправителя и при выключенном SMTP: заглушка в .invalid.
      printf 'localhost:25' > "$out/.smtp_host.new"
      printf 'unset' > "$out/.smtp_user.new"
      printf 'unset' > "$out/.smtp_password.new"
      printf 'alerts@antec.invalid' > "$out/.smtp_from_address.new"
    else
      cat "$out/.smtp.err" >&2
      echo "render: не удалось прочитать antec/smtp; прежние секреты Grafana сохранены" >&2
      exit 1
    fi
    for f in oauth_client_secret admin_password db_password smtp_enabled smtp_host smtp_user \
        smtp_password smtp_from_address; do
      replace "$out/.$f.new" "$out/$f"
    done
    # Пользователь образа grafana: uid 472, группа root (gid 0).
    owner=472:0
    ;;
  *)
    echo "render: использование: render.sh postgres|keycloak|litellm|open-webui|platform-api|platform-migrate|platform-worker|platform-subject|grafana" >&2
    exit 2
    ;;
esac

chown -R "$owner" "$out"
find "$out" -type f -exec chmod 0400 {} +
find "$out" -type d -exec chmod 0700 {} +
echo "render: секреты $target выгружены" >&2
if [ "$changed" = 1 ]; then echo changed; fi
