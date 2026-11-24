#!/usr/bin/env bash
# Права субъекта данных (ADR-0012, п. 7). Вывод export содержит персональные данные.
#   subject.sh find                 id пользователя по email (email читается со stdin)
#   subject.sh export <id> [--open-webui-uid <antec_uid> | --skip-open-webui] > файл.json
#   subject.sh erase  <id> [--open-webui-uid <antec_uid> | --skip-open-webui]
#   subject.sh reapply              повторить удаления из журнала после восстановления из бэкапа
# Журнал удалений вне БД: бэкап, снятый до удаления, вернёт данные, и их надо удалить снова.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

LEDGER="${ANTEC_ERASURE_LEDGER:-$HOME/Library/Application Support/antec/erasures.log}"
# Сборка отдельно и в stderr: stdout команды только JSON.
subject() {
  compose --profile bootstrap build --quiet platform-subject >&2
  docker compose --progress quiet --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/compose.yaml" \
    --profile bootstrap run --rm -T --pull never platform-subject subject "$@"
}
usage() { die "использование: subject.sh find | export <id> | erase <id> | reapply"; }

case "${1:-}" in
  find)
    subject find ;;
  export)
    [[ -n "${2:-}" ]] || usage
    subject export --user "${@:2}" </dev/null ;;
  erase)
    [[ -n "${2:-}" ]] || usage
    umask 077
    mkdir -p "$(dirname "$LEDGER")"
    log "удаление данных пользователя $2 необратимо"
    read -r -p "Повторите id для подтверждения: " confirm
    [[ "$confirm" == "$2" ]] || die "id не совпадает, ничего не удалено"
    # До удаления: оборванное удаление тоже надо повторить. Строка = аргументы команды.
    printf '%s\n' "${*:2}" >> "$LEDGER"
    subject erase --user "${@:2}" </dev/null ;;
  reapply)
    [[ -f "$LEDGER" ]] || die "журнала удалений нет: $LEDGER"
    failed=0
    while read -r -u 3 -a entry; do
      [[ "${#entry[@]}" -gt 0 ]] || continue
      log "повторное удаление ${entry[0]}"
      subject erase --user "${entry[@]}" </dev/null || { failed=1; log "не выполнено: ${entry[0]}"; }
    done 3< "$LEDGER"
    exit "$failed" ;;
  *) usage ;;
esac
