#!/usr/bin/env bash
# Компиляция и тесты политик Cerbos ядра и инсталляции вместе (ADR-0008, п. 4), тем же
# образом по digest, что и на стенде. Дерево как в сервисе cerbos: /policies/core и
# /policies/instance.
#   policies-check.sh [каталог инсталляции]   по умолчанию instances/dev ядра
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
core_dir="$(cd "$script_dir/../../.." && pwd)"
instance_dir="$(cd "${1:-$core_dir/instances/dev}" && pwd)"
image="$(grep -Eo 'ghcr\.io/cerbos/cerbos:[0-9.]+@sha256:[0-9a-f]{64}' "$core_dir/deploy/compose/compose.yaml" | head -1)"
[[ -n "$image" ]] || { echo "образ Cerbos не найден в compose.yaml" >&2; exit 1; }
mounts=(-v "$core_dir/policies/cerbos:/policies/core:ro")
if [[ -d "$instance_dir/policies" ]]; then
  mounts+=(-v "$instance_dir/policies:/policies/instance:ro")
fi
# tmpfs-корень /policies: при --read-only точки монтирования core и instance иначе негде создать.
exec docker run --rm --network none -u 65534:65534 --read-only \
  --tmpfs /policies:size=1m,mode=0555 "${mounts[@]}" "$image" \
  compile --verbose /policies
