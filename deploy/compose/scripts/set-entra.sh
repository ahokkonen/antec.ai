#!/usr/bin/env bash
# Параметры приложения Entra ID для брокера Keycloak (ADR-0007): Tenant ID, Client ID и
# client secret. Сохраняются в OpenBao (antec/entra); секрет вводится без эха и передаётся
# только через stdin. После сохранения стенд перенастраивается через up.sh.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

[[ "$(bao_status_code)" == "0" ]] || die "OpenBao не запущен или запечатан; сначала scripts/up.sh"
installation="$(current_installation)"
BAO_TOKEN="$(kc_get "$(kc_root_name "$installation")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

guid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
read -r -p "Directory (tenant) ID: " tenant_id
[[ "$tenant_id" =~ $guid_re ]] || die "Tenant ID должен быть GUID"
read -r -p "Application (client) ID: " client_id
[[ "$client_id" =~ $guid_re ]] || die "Client ID должен быть GUID"
read -r -s -p "Client secret (ввод не отображается): " client_secret; echo >&2
[[ -n "$client_secret" ]] || die "пустой client secret"

# JSON собирается в python3 из stdin: секрет не попадает в argv.
printf '%s\n%s\n%s\n' "$tenant_id" "$client_id" "$client_secret" \
  | python3 -c '
import json, sys
tenant, client, secret = sys.stdin.read().split("\n")[:3]
print(json.dumps({"tenant_id": tenant, "client_id": client, "client_secret": secret}))' \
  | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec entra - >/dev/null
unset client_secret BAO_TOKEN
log "параметры Entra ID сохранены в OpenBao (antec/entra)"

"$(dirname "$0")/up.sh"
# keycloak.conf и vault-файлы читаются при старте: перезапуск применяет новый секрет.
log "перезапуск Keycloak с новым секретом Entra"
compose restart keycloak >/dev/null
compose up -d --wait keycloak
log "готово: вход через Entra на http://localhost:8080/realms/antec/account"
