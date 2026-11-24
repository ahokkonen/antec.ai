#!/usr/bin/env bash
# Контрольная точка аудита (ADR-0012, п. 4): проверка, подпись OpenBao Transit, запись вне БД.
# На стенде вызывают up.sh и backup.sh; на сервере таймер раз в час и WORM-хранилище.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain
BAO_TOKEN="$(kc_get "$(kc_root_name "$(current_installation)")")"; export BAO_TOKEN
audit_checkpoint
