#!/usr/bin/env bash
# Запуск локального стенда: OpenBao (init/unseal), секреты, PostgreSQL, Keycloak. Идемпотентен.
#   --recreate-realm   удалить realm antec и создать заново из keycloak/realm-antec.json
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

recreate_realm=""
case "${1:-}" in
  "") ;;
  --recreate-realm) recreate_realm="--recreate-realm" ;;
  *) die "использование: up.sh [--recreate-realm]" ;;
esac

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

for policy in render-postgres render-keycloak render-litellm render-open-webui; do
  compose exec -T -e BAO_TOKEN openbao bao policy write "$policy" - \
    < "$COMPOSE_DIR/openbao/policies/$policy.hcl" >/dev/null
done

if ! bao_exec kv get -mount=antec postgres >/dev/null 2>&1; then
  log "генерация паролей PostgreSQL в OpenBao (antec/postgres)"
  python3 - <<'PY' | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec postgres - >/dev/null
import json, secrets
keys = ["superuser", "keycloak", "litellm", "openwebui", "platform", "dbos"]
print(json.dumps({k: secrets.token_urlsafe(32) for k in keys}))
PY
fi

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
# Open WebUI (ADR-0002, ADR-0018): секрет сессий, виртуальный ключ LiteLLM; секрет клиента
# open-webui берётся из antec/keycloak, где его задаёт realm как код.
ensure_generated_secrets open-webui webui_secret_key litellm_key:sk-
copy_db_password openwebui open-webui
copy_secret_field keycloak open_webui_client_secret open-webui oauth_client_secret

# Короткоживущие токены только для secrets-render, передаются через stdin.
render() { # цель
  log "выгрузка секретов $1 (secrets-render)"
  bao_exec token create -policy="render-$1" -ttl=5m -use-limit=10 \
      -orphan -no-default-policy -field=token \
    | compose --profile bootstrap run --rm -T secrets-render "$1"
}
render postgres
render keycloak
render litellm
render open-webui

log "сборка и запуск PostgreSQL"
compose up -d --build --wait postgres
log "сборка и запуск Keycloak (первый запуск около минуты)"
compose up -d --build --wait keycloak

log "настройка Keycloak: master, realm antec, брокер Entra"
keycloak_inputs | python3 "$COMPOSE_DIR/scripts/keycloak.py" apply $recreate_realm

log "запуск LiteLLM (миграции БД при первом запуске, около минуты)"
compose up -d --wait litellm
log "подготовка LiteLLM: бюджет пользователей и ключ Open WebUI"
litellm_provision_inputs \
  | compose exec -T litellm /app/.venv/bin/python -c "$(cat "$COMPOSE_DIR/scripts/litellm_provision.py")"

log "запуск Open WebUI (миграции БД при первом запуске)"
compose up -d --wait open-webui
unset BAO_TOKEN
log "стенд запущен: $(compose ps --format '{{.Service}}={{.State}}' | tr '\n' ' ')"
log "Keycloak: http://localhost:8080/realms/antec/account (администрирование: http://localhost:8080/admin)"
log "Open WebUI: http://localhost:3000 (вход через Keycloak)"
