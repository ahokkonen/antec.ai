#!/bin/sh
# Обёртка entrypoint для сервисов, которые читают секреты только из окружения (LiteLLM,
# Open WebUI; ADR-0018). Файлы, которые secrets-render выгрузил из OpenBao, переносятся в
# окружение этого процесса непосредственно перед запуском: в конфигурации контейнера
# (docker inspect), в argv и в образе секретов нет.
#   env-from-files.sh <команда> [аргументы...]
# Имя файла = имя переменной; допускаются только [A-Z][A-Z0-9_]*.
set -eu

[ "$#" -gt 0 ] || { echo "antec: использование: env-from-files.sh <команда> [аргументы...]" >&2; exit 2; }
dir=/run/antec-secrets
[ -d "$dir" ] || { echo "antec: нет каталога секретов $dir; запускайте стенд через scripts/up.sh" >&2; exit 1; }
for f in "$dir"/*; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  case "$name" in
    *[!A-Z0-9_]*|[!A-Z]*) echo "antec: пропущен файл с недопустимым именем: $name" >&2; continue ;;
  esac
  value="$(cat "$f")"
  export "$name=$value"
done
unset f name value
exec "$@"
