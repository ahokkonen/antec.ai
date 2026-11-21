#!/usr/bin/env bash
# Запуск локального стенда: OpenBao (init/unseal), секреты, PostgreSQL. Идемпотентен.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

log "запуск OpenBao"
compose up -d openbao
wait_for_openbao_process

initialized="$(compose exec -T openbao bao status -format=json 2>/dev/null | json_field initialized || true)"
if [[ "$initialized" != "True" ]]; then
  if [[ -n "$(kc_get "$KC_UNSEAL")" ]]; then
    die "OpenBao не инициализирован, но в Связке ключей есть ключ от прошлой установки. \
Если данные удалены намеренно, выполните scripts/down.sh --purge"
  fi
  log "первичная инициализация OpenBao (1 доля ключа, только для локального стенда)"
  init_json="$(compose exec -T openbao bao operator init -key-shares=1 -key-threshold=1 -format=json)"
  unseal_key="$(printf '%s' "$init_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_keys_b64"][0])')"
  root_token="$(printf '%s' "$init_json" | json_field root_token)"
  unset init_json
  kc_set "$KC_UNSEAL" "$unseal_key"
  kc_set "$KC_ROOT" "$root_token"
  unset unseal_key root_token
  log "ключ распечатывания и root token сохранены в Связке ключей ($KEYCHAIN_ACCOUNT)"
fi

if [[ "$(bao_status_code)" == "2" ]]; then
  log "распечатывание OpenBao"
  # Ключ через stdin (`key=-`), не через argv ни на хосте, ни в контейнере.
  kc_get "$KC_UNSEAL" | tr -d '\n' | compose exec -T openbao bao write sys/unseal key=- >/dev/null
fi
[[ "$(bao_status_code)" == "0" ]] || die "OpenBao остался запечатан"

BAO_TOKEN="$(kc_get "$KC_ROOT")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

if ! bao_exec secrets list -format=json | python3 -c 'import json,sys; sys.exit(0 if "antec/" in json.load(sys.stdin) else 1)'; then
  log "включение KV v2 на antec/"
  bao_exec secrets enable -path=antec -version=2 kv >/dev/null
fi

compose exec -T -e BAO_TOKEN openbao bao policy write render-postgres - \
  < "$COMPOSE_DIR/openbao/policies/render-postgres.hcl" >/dev/null

if ! bao_exec kv get -mount=antec postgres >/dev/null 2>&1; then
  log "генерация паролей PostgreSQL в OpenBao (antec/postgres)"
  python3 - <<'PY' | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec postgres - >/dev/null
import json, secrets
keys = ["superuser", "keycloak", "litellm", "openwebui", "platform", "dbos"]
print(json.dumps({k: secrets.token_urlsafe(32) for k in keys}))
PY
fi

log "выгрузка секретов PostgreSQL (secrets-render)"
# Короткоживущий токен только для secrets-render, передаётся через stdin.
bao_exec token create -policy=render-postgres -ttl=5m -use-limit=10 \
    -orphan -no-default-policy -field=token \
  | compose --profile bootstrap run --rm -T secrets-render
unset BAO_TOKEN

log "сборка и запуск PostgreSQL"
compose up -d --build --wait postgres
log "стенд запущен: $(compose ps --format '{{.Service}}={{.State}}' | tr '\n' ' ')"
