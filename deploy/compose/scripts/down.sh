#!/usr/bin/env bash
# Остановка стенда. Данные (тома) сохраняются. --purge удаляет тома и ключи из Связки ключей.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

if [[ "${1:-}" == "--purge" ]]; then
  read -r -p "Удалить ВСЕ данные стенда (PostgreSQL, OpenBao) и ключи из Связки ключей? Введите 'purge': " answer
  [[ "$answer" == "purge" ]] || die "отменено"
  compose down --volumes --remove-orphans
  kc_delete "$KC_UNSEAL"
  kc_delete "$KC_ROOT"
  log "стенд и данные удалены"
else
  compose down --remove-orphans
  log "стенд остановлен, данные сохранены"
fi
