#!/usr/bin/env bash
# SMTP для алертов Grafana (ADR-0013, п. 6): сохраняется в OpenBao (antec/smtp), пароль вводится
# без эха и передаётся только через stdin. После сохранения up.sh выгружает секреты Grafana
# заново и перезапускает её. Получатель алертов задаётся в манифесте (observability.alert_email).
#   set-smtp.sh            ввод host:port, пользователя, адреса отправителя и пароля
#   set-smtp.sh --remove   удалить настройки SMTP (алерты только в Grafana)
# Соединение только с STARTTLS (GF_SMTP_STARTTLS_POLICY=MandatoryStartTLS).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require_macos_keychain

mode=set
case "${1:-}" in
  "") ;;
  --remove) mode=remove ;;
  *) die "использование: set-smtp.sh [--remove]" ;;
esac

[[ "$(bao_status_code)" == "0" ]] || die "OpenBao не запущен или запечатан; сначала scripts/up.sh"
installation="$(current_installation)"
BAO_TOKEN="$(kc_get "$(kc_root_name "$installation")")"
[[ -n "$BAO_TOKEN" ]] || die "нет root token в Связке ключей"
export BAO_TOKEN

if [[ "$mode" == remove ]]; then
  bao_exec kv metadata delete -mount=antec smtp >/dev/null
  unset BAO_TOKEN
  log "настройки SMTP удалены (antec/smtp)"
else
  read -r -p "SMTP host:port (например smtp.example.fi:587): " host
  [[ "$host" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] || die "ожидается host:port"
  read -r -p "Пользователь SMTP: " user
  [[ -n "$user" && "$user" =~ ^[^[:space:]]+$ ]] || die "пустой пользователь или пробелы"
  read -r -p "Адрес отправителя: " from_address
  [[ "$from_address" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "некорректный адрес"
  read -r -s -p "Пароль SMTP (ввод не отображается): " password; echo >&2
  [[ -n "$password" ]] || die "пустой пароль"
  # Все поля одной записью (put): JSON через stdin, пароль не попадает в argv.
  python3 -c 'import json, sys; print(json.dumps(dict(zip(["host", "user", "from_address", "password"], sys.stdin.read().split("\0")))))' \
    < <(printf '%s\0%s\0%s\0%s' "$host" "$user" "$from_address" "$password") \
    | compose exec -T -e BAO_TOKEN openbao bao kv put -mount=antec smtp - >/dev/null
  unset password BAO_TOKEN
  log "SMTP сохранён в OpenBao (antec/smtp: $host, $user, $from_address)"
fi

# up.sh выгрузит секреты Grafana и перезапустит её, если они изменились.
"$(dirname "$0")/up.sh"
