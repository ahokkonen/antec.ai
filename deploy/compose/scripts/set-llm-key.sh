#!/usr/bin/env bash
# Ключ API провайдера LLM для LiteLLM (ADR-0018): сохраняется в OpenBao (antec/llm-providers),
# вводится без эха и передаётся только через stdin. После сохранения up.sh выгружает секреты
# заново и перезапускает LiteLLM.
#   set-llm-key.sh anthropic|openai|openrouter
# У провайдера должен стоять лимит расходов: ключ шлюза даёт доступ ко всем его моделям.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

case "${1:-}" in
  anthropic|openai|openrouter) provider="$1" ;;
  *) die "использование: set-llm-key.sh anthropic|openai|openrouter" ;;
esac
field="${provider}_api_key"

[[ "$(bao_status_code)" == "0" ]] || die "OpenBao не запущен или запечатан; сначала scripts/up.sh"
installation="$(current_installation)"
BAO_TOKEN="$(kc_get "$(kc_root_name "$installation")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

read -r -s -p "API-ключ $provider (ввод не отображается): " api_key; echo >&2
[[ -n "$api_key" ]] || die "пустой ключ"
[[ "$api_key" =~ ^[A-Za-z0-9._-]+$ ]] || die "неожиданные символы в ключе"

# Запись дополняется (patch), если она уже есть, иначе создаётся. Ключ идёт только через stdin.
if [[ "$(bao_kv_json llm-providers)" == "null" ]]; then
  printf '%s' "$api_key" | compose exec -T -e BAO_TOKEN openbao \
    bao kv put -mount=antec llm-providers "$field=-" >/dev/null
else
  printf '%s' "$api_key" | compose exec -T -e BAO_TOKEN openbao \
    bao kv patch -mount=antec llm-providers "$field=-" >/dev/null
fi
unset api_key BAO_TOKEN
log "ключ $provider сохранён в OpenBao (antec/llm-providers:$field)"

# up.sh выгрузит новый ключ и перезапустит LiteLLM, если ключ изменился.
"$(dirname "$0")/up.sh"
log "готово: проверка scripts/litellm-smoke.sh"
