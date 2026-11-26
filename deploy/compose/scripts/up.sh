#!/usr/bin/env bash
# Запуск локального стенда: OpenBao (init/unseal), секреты, PostgreSQL, Keycloak, LiteLLM,
# Platform API, Open WebUI. Идемпотентен. Инсталляция: ANTEC_INSTANCE_DIR (по умолчанию
# instances/dev ядра, ADR-0008).
#   --recreate-realm   удалить realm antec и создать заново из keycloak/realm-antec.json
#   --no-backup        не делать бэкап перед миграциями БД platform (только стенд разработки,
#                      ADR-0014, п. 7)
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

recreate_realm=""
no_backup=""
for arg in "$@"; do
  case "$arg" in
    --recreate-realm) recreate_realm="--recreate-realm" ;;
    --no-backup) no_backup=1 ;;
    *) die "использование: up.sh [--recreate-realm] [--no-backup]" ;;
  esac
done

log "проверка и рендер манифеста инсталляции $ANTEC_INSTANCE_DIR (ADR-0008)"
render_instance
remember_active_instance

log "запуск OpenBao"
compose up -d openbao
wait_for_openbao_process

installation="$(current_installation)"
initialized="$(compose exec -T openbao bao status -format=json 2>/dev/null | json_field initialized || true)"
if [[ "$initialized" != "True" ]]; then
  if [[ -n "$installation" ]]; then
    die "OpenBao не инициализирован, но в Связке ключей отмечена установка $installation. \
Если её данные удалены намеренно, выполните scripts/down.sh --purge"
  fi
  installation="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  log "первичная инициализация OpenBao (1 доля ключа, только для локального стенда)"
  init_json="$(compose exec -T openbao bao operator init -key-shares=1 -key-threshold=1 -format=json)"
  unseal_key="$(printf '%s' "$init_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_keys_b64"][0])')"
  root_token="$(printf '%s' "$init_json" | json_field root_token)"
  unset init_json
  kc_set "$(kc_unseal_name "$installation")" "$unseal_key"
  kc_set "$(kc_root_name "$installation")" "$root_token"
  kc_set "$KC_INSTALLATION" "$installation"
  unset unseal_key root_token
  log "ключ распечатывания и root token сохранены в Связке ключей (установка $installation)"
fi

if [[ "$(bao_status_code)" == "2" ]]; then
  log "распечатывание OpenBao"
  # Ключ через stdin (`key=-`), не через argv ни на хосте, ни в контейнере.
  kc_get "$(kc_unseal_name "$installation")" | tr -d '\n' \
    | compose exec -T openbao bao write sys/unseal key=- >/dev/null
fi
[[ "$(bao_status_code)" == "0" ]] || die "OpenBao остался запечатан"
wait_for_openbao_active

BAO_TOKEN="$(kc_get "$(kc_root_name "$installation")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

if ! bao_exec secrets list -format=json | python3 -c 'import json,sys; sys.exit(0 if "antec/" in json.load(sys.stdin) else 1)'; then
  log "включение KV v2 на antec/"
  bao_exec secrets enable -path=antec -version=2 kv >/dev/null
fi

if ! bao_exec secrets list -format=json | python3 -c 'import json,sys; sys.exit(0 if "transit/" in json.load(sys.stdin) else 1)'; then
  log "включение Transit и ключ подписи контрольных точек аудита"
  bao_exec secrets enable transit >/dev/null
fi
# Не экспортируется и не удаляется (значения по умолчанию); повторная запись ключ не меняет.
bao_exec write -f "transit/keys/$AUDIT_KEY" type=ed25519 >/dev/null

for policy in audit-checkpoint render-postgres render-keycloak render-litellm render-open-webui \
    render-platform-api render-platform-migrate render-platform-worker render-platform-subject; do
  compose exec -T -e BAO_TOKEN openbao bao policy write "$policy" - \
    < "$COMPOSE_DIR/openbao/policies/$policy.hcl" >/dev/null
done

if ! bao_exec kv get -mount=antec postgres >/dev/null 2>&1; then
  log "генерация паролей PostgreSQL в OpenBao (antec/postgres)"
  python3 - <<'PY' | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec postgres - >/dev/null
import json, secrets
keys = ["superuser", "keycloak", "litellm", "openwebui", "platform", "dbos", "platform_app", "platform_worker", "platform_subject"]
print(json.dumps({k: secrets.token_urlsafe(32) for k in keys}))
PY
fi

# Роли Platform API (ADR-0006, п. 9; ADR-0012, п. 2) появились позже первых установок.
ensure_generated_secrets postgres platform_app platform_worker platform_subject

# Секреты сервисов в OpenBao. Недостающие ключи дополняются, существующие не меняются;
# ошибка чтения прерывает запуск (bao_kv_json). Пароль роли БД копируется из antec/postgres,
# чтобы токен выгрузки сервиса не имел доступа к паролям других ролей.
ensure_generated_secrets keycloak bootstrap_client_secret automation_client_secret \
  admin_password breakglass_password platform_api_client_secret open_webui_client_secret
copy_db_password keycloak keycloak
# LiteLLM (ADR-0018): мастер-ключ с префиксом sk- (формат ключей LiteLLM); salt key шифрует
# учётные данные в БД LiteLLM и не должен меняться после первого запуска.
ensure_generated_secrets litellm master_key:sk- salt_key
copy_db_password litellm litellm
# Open WebUI (ADR-0002, ADR-0018): секрет сессий; секрет клиента open-webui берётся из
# antec/keycloak, где его задаёт realm как код. Ключа LiteLLM у Open WebUI нет (ADR-0006).
ensure_generated_secrets open-webui webui_secret_key
copy_db_password openwebui open-webui
copy_secret_field keycloak open_webui_client_secret open-webui oauth_client_secret
# Platform API (ADR-0006, п. 10): ключ LiteLLM и пароль роли platform_app у сервиса, пароль
# владельца схемы platform только у одноразовых миграций.
ensure_generated_secrets platform-api litellm_key:sk-
copy_db_password platform_app platform-api
# Обмен токена для MCP-серверов (ADR-0010): секрет клиента platform-api задаёт realm как код.
copy_secret_field keycloak platform_api_client_secret platform-api oidc_client_secret
copy_db_password platform platform-migrate
copy_db_password platform_worker platform-worker
copy_db_password platform_subject platform-subject

# Сервисы, работавшие до этого запуска: их нужно перезапустить, если изменились их секреты
# (обёртки и Keycloak читают файлы секретов только при старте процесса).
running_before="$(compose ps --status running --services 2>/dev/null || true)"
secrets_changed=""

# Короткоживущие токены только для secrets-render, передаются через stdin.
render() { # цель
  log "выгрузка секретов $1 (secrets-render)"
  local out
  out="$(bao_exec token create -policy="render-$1" -ttl=5m -use-limit=10 \
      -orphan -no-default-policy -field=token \
    | compose --profile bootstrap run --rm -T secrets-render "$1")"
  if grep -qx changed <<<"$out"; then secrets_changed+=" $1"; fi
}

# Запускает сервис и перезапускает его, если он уже работал, а его секреты изменились.
start_service() { # сервис [аргументы compose up]
  local svc="$1"; shift
  compose up -d "$@" --wait "$svc"
  if [[ " $secrets_changed " == *" $svc "* ]] && grep -qx "$svc" <<<"$running_before"; then
    log "секреты $svc изменились: перезапуск"
    compose restart "$svc" >/dev/null
    compose up -d --wait "$svc"
  fi
}
render postgres
render keycloak
render litellm
render open-webui
render platform-api
render platform-migrate
render platform-worker
render platform-subject

log "сборка и запуск PostgreSQL"
postgres_before="$(compose ps -q postgres 2>/dev/null || true)"
compose up -d --build --wait postgres
# Пересозданный PostgreSQL рвёт соединения: пул Keycloak отдаёт их закрытыми ещё некоторое время,
# и настройка realm ниже не получает токен. Остальные сервисы переподключаются сами.
if [[ -n "$postgres_before" && "$(compose ps -q postgres)" != "$postgres_before" ]] \
    && grep -qx keycloak <<<"$running_before"; then
  log "PostgreSQL пересоздан: перезапуск Keycloak"
  compose restart keycloak >/dev/null
fi
ensure_platform_role platform_app 30
ensure_platform_role platform_worker 5 openwebui
ensure_platform_role platform_subject 5 openwebui
log "сборка и запуск Keycloak (первый запуск около минуты)"
start_service keycloak --build

log "настройка Keycloak: master, realm antec, брокер Entra"
keycloak_inputs | python3 "$COMPOSE_DIR/scripts/keycloak.py" apply $recreate_realm

plugins="$(instance_services | tr '\n' ' ')"
if [[ -n "${plugins// /}" ]]; then
  log "сборка и запуск плагинов инсталляции: $plugins"
  # shellcheck disable=SC2086
  compose up -d --build --wait $plugins
fi

log "запуск LiteLLM (миграции БД при первом запуске, около минуты)"
start_service litellm
log "подготовка LiteLLM: бюджет пользователей и ключ Platform API"
litellm_provision_inputs \
  | compose exec -T litellm /app/.venv/bin/python -c "$(cat "$COMPOSE_DIR/scripts/litellm_provision.py")"

log "запуск Cerbos (политики ядра и инсталляции)"
compose up -d --wait cerbos

log "сборка Platform API и миграции БД platform"
compose build platform-api
# План до миграций (ADR-0014, п. 7): понижение версии ядра останавливает запуск, ожидающие
# миграции существующей БД выполняются только после бэкапа.
plan_rc=0
plan_json="$(compose --profile bootstrap run --rm -T platform-migrate migrate --plan)" || plan_rc=$?
case "$plan_rc" in
  0) log "  миграций нет" ;;
  3)
    plan_field() { python3 -c 'import json,sys; v=json.load(sys.stdin)[sys.argv[1]]; print(" ".join(v) if isinstance(v, list) else (v or ""))' "$1" <<<"$plan_json"; }
    current="$(plan_field current)"
    log "  миграции: $(plan_field pending)${current:+ (сейчас $current)}"
    irreversible="$(plan_field irreversible)"
    [[ -z "$irreversible" ]] || log "  необратимые: $irreversible"
    if [[ -z "$current" ]]; then
      log "  новая БД: бэкап не нужен"
    elif [[ -n "$no_backup" ]]; then
      log "  --no-backup: миграции без бэкапа (только стенд разработки)"
    else
      log "бэкап перед миграциями"
      "$COMPOSE_DIR/scripts/backup.sh"
    fi
    ;;
  4) die "БД platform на ревизии, неизвестной этой версии ядра ($plan_json): понижение версии \
не поддерживается, восстановите бэкап, сделанный до обновления (restore-verify.sh, ADR-0014)" ;;
  *) die "не удалось получить план миграций БД platform (код $plan_rc)" ;;
esac
compose --profile bootstrap run --rm -T platform-migrate
# Схема аудита с отдельным владельцем (ADR-0012, п. 4). Перед первым переносом сервисы,
# пишущие в прежнюю таблицу, останавливаются: их итоги успевают записаться.
if [[ "$(pg_exec psql -X -At -d platform -c "select to_regclass('audit.audit_events') is null")" == t ]]; then
  log "перенос аудита в схему audit: остановка Platform API и platform-worker"
  compose stop -t 60 platform-api platform-worker >/dev/null
fi
pg_exec psql -X -q -v ON_ERROR_STOP=1 -d platform < "$COMPOSE_DIR/postgres/audit.sql" >/dev/null
log "запуск Platform API"
start_service platform-api

log "запуск Open WebUI (миграции БД при первом запуске)"
start_service open-webui
# Таблицы создают миграции Open WebUI, поэтому права и worker после его запуска (ADR-0012).
pg_exec psql -X -q -d openwebui < "$COMPOSE_DIR/open-webui/retention-grants.sql" >/dev/null
log "запуск platform-worker"
start_service platform-worker
log "сверка аккаунтов Open WebUI с Keycloak"
sync_open_webui_identities
audit_checkpoint
unset BAO_TOKEN
log "стенд запущен: $(compose ps --format '{{.Service}}={{.State}}' | tr '\n' ' ')"
log "Keycloak: http://localhost:8080/realms/antec/account (администрирование: http://localhost:8080/admin)"
log "Open WebUI: http://localhost:3000 (вход через Keycloak)"
