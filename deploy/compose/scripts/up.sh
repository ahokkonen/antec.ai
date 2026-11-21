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

BAO_TOKEN="$(kc_get "$(kc_root_name "$installation")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

if ! bao_exec secrets list -format=json | python3 -c 'import json,sys; sys.exit(0 if "antec/" in json.load(sys.stdin) else 1)'; then
  log "включение KV v2 на antec/"
  bao_exec secrets enable -path=antec -version=2 kv >/dev/null
fi

for policy in render-postgres render-keycloak; do
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

# Секреты Keycloak (ADR-0007). Недостающие ключи дополняются, существующие не меняются.
# Генерация только при подтверждённом отсутствии ключей; ошибка чтения прерывает запуск.
existing="$(bao_kv_json keycloak)"
if ! printf '%s' "$existing" | python3 -c '
import json, sys
need = {"bootstrap_client_secret", "automation_client_secret", "admin_password",
        "breakglass_password", "platform_api_client_secret", "open_webui_client_secret"}
doc = json.load(sys.stdin) or {}
have = set(((doc.get("data") or {}).get("data") or {}).keys())
sys.exit(0 if need <= have else 1)'; then
  log "генерация секретов Keycloak в OpenBao (antec/keycloak)"
  printf '%s' "$existing" | python3 -c '
import json, secrets, sys
doc = json.load(sys.stdin) or {}
data = dict(((doc.get("data") or {}).get("data") or {}))
for k in ("bootstrap_client_secret", "automation_client_secret", "admin_password",
          "breakglass_password", "platform_api_client_secret", "open_webui_client_secret"):
    data.setdefault(k, secrets.token_urlsafe(32))
print(json.dumps(data))' | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec keycloak - >/dev/null
fi
unset existing
# Пароль роли keycloak копируется из antec/postgres, чтобы токен выгрузки Keycloak
# не имел доступа к паролям других ролей.
bao_exec kv get -mount=antec -field=keycloak postgres \
  | compose exec -T -e BAO_TOKEN openbao bao kv patch -mount=antec keycloak db_password=- >/dev/null

# Короткоживущие токены только для secrets-render, передаются через stdin.
render() { # цель
  log "выгрузка секретов $1 (secrets-render)"
  bao_exec token create -policy="render-$1" -ttl=5m -use-limit=10 \
      -orphan -no-default-policy -field=token \
    | compose --profile bootstrap run --rm -T secrets-render "$1"
}
render postgres
render keycloak

log "сборка и запуск PostgreSQL"
compose up -d --build --wait postgres
log "сборка и запуск Keycloak (первый запуск около минуты)"
compose up -d --build --wait keycloak

log "настройка Keycloak: master, realm antec, брокер Entra"
keycloak_inputs | python3 "$COMPOSE_DIR/scripts/keycloak.py" apply $recreate_realm
unset BAO_TOKEN
log "стенд запущен: $(compose ps --format '{{.Service}}={{.State}}' | tr '\n' ' ')"
log "Keycloak: http://localhost:8080/realms/antec/account (администрирование: http://localhost:8080/admin)"
