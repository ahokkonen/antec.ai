#!/usr/bin/env bash
# Остановка стенда. Данные (тома) сохраняются.
#   --purge               удалить тома стенда. Ключи OpenBao этой установки остаются в
#                         Связке ключей: без них её бэкапы нельзя восстановить.
#   --purge --forget-keys удалить и ключи; бэкапы этой установки станут невосстановимыми.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

case "${1:-} ${2:-}" in
  "--purge --forget-keys"|"--forget-keys --purge") mode=forget ;;
  "--purge ") mode=purge ;;
  " ") mode=stop ;;
  *) die "использование: down.sh [--purge [--forget-keys]]" ;;
esac

if [[ "$mode" == "stop" ]]; then
  compose --profile bootstrap --profile observability down --remove-orphans
  log "стенд остановлен, данные сохранены"
  exit 0
fi

require_macos_keychain
installation="$(current_installation)"
echo "Будут удалены ВСЕ данные стенда (все тома: $(compose config --volumes | tr '\n' ' ')), установка: ${installation:-неизвестна}." >&2
if [[ "$mode" == "forget" ]]; then
  echo "Также будут удалены ключ распечатывания и root token этой установки." >&2
  echo "ВНИМАНИЕ: снапшоты OpenBao из бэкапов этой установки станут невосстановимыми." >&2
  confirm="forget"
else
  echo "Ключи этой установки останутся в Связке ключей: бэкапы восстанавливаются через" >&2
  echo "restore-verify.sh (установка указана в manifest.json)." >&2
  confirm="purge"
fi
read -r -p "Введите '$confirm' для подтверждения: " answer
[[ "$answer" == "$confirm" ]] || die "отменено"

# --profile bootstrap и observability: тома одноразовых сервисов (platform-migrate) и телеметрии тоже удаляются.
compose --profile bootstrap --profile observability down --volumes --remove-orphans
kc_delete "$KC_INSTALLATION"
if [[ "$mode" == "forget" && -n "$installation" ]]; then
  kc_delete "$(kc_unseal_name "$installation")"
  kc_delete "$(kc_root_name "$installation")"
  log "стенд, данные и ключи установки $installation удалены"
else
  log "стенд и данные удалены; ключи установки ${installation:-?} сохранены для её бэкапов"
fi
