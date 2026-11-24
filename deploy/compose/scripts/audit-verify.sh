#!/usr/bin/env bash
# Проверка аудита стенда (ADR-0012, п. 4): подписи контрольных точек, цепочка хэшей, соответствие
# точкам. Код 1 = аудит изменён или обрезан. Бэкап старше точек после восстановления тоже даёт
# «обрезано»: это ожидаемо, файл точек восстанавливается вместе с бэкапом.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain
BAO_TOKEN="$(kc_get "$(kc_root_name "$(current_installation)")")"; export BAO_TOKEN
audit_verify
log "аудит проверен"
