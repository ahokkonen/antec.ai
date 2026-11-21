#!/bin/sh
# Обёртка entrypoint LiteLLM (ADR-0018). Интеграция LiteLLM с Vault/OpenBao платная, в OSS
# секреты читаются только из окружения (os.environ/). Поэтому файлы, которые secrets-render
# выгрузил из OpenBao, переносятся в окружение этого процесса непосредственно перед запуском:
# в конфигурации контейнера (docker inspect), в argv и в образе секретов нет.
# Имя файла = имя переменной; допускаются только [A-Z][A-Z0-9_]*.
set -eu

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
exec /app/docker/prod_entrypoint.sh "$@"
