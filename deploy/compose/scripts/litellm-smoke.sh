#!/usr/bin/env bash
# Проверка LiteLLM на стенде (ADR-0018): scripts/litellm_smoke.py выполняется интерпретатором
# контейнера litellm (порт на хост не публикуется). Мастер-ключ передаётся через stdin, код
# скрипта через argv (секретов в нём нет). Ожидаемые модели: манифест инсталляции (ADR-0008). Код выхода: 0 всё ок, 1 ошибка, 2 нет ключа провайдера.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

[[ "$(bao_status_code)" == "0" ]] || die "OpenBao не запущен или запечатан; сначала scripts/up.sh"
BAO_TOKEN="$(kc_get "$(kc_root_name "$(current_installation)")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

log "проверка LiteLLM (вызовы chat-fast стоят денег: несколько сотых цента)"
bao_kv_json litellm \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"master_key": d["data"]["data"]["master_key"], "models": json.loads(sys.argv[1])}))' "$(instance_models_json)" \
  | compose exec -T litellm /app/.venv/bin/python -c "$(cat "$COMPOSE_DIR/scripts/litellm_smoke.py")"
