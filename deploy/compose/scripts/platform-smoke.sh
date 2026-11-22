#!/usr/bin/env bash
# Проверка Platform API на стенде (ADR-0006): вход через браузер (клиент antec-smoke), вызовы
# фасада изнутри контейнера platform-api, затем записи в БД: вызовы моделей со статусом и
# токенами, история, аудит в platform и конечный пользователь в учёте LiteLLM.
# Вызовы chat-fast стоят доли цента.
#   --no-open   не открывать браузер, только напечатать URL входа
set -euo pipefail
source "$(dirname "$0")/lib.sh"

open_flag=()
case "${1:-}" in
  "") ;;
  --no-open) open_flag=(--no-open) ;;
  *) die "использование: platform-smoke.sh [--no-open]" ;;
esac

log "вход пользователя через браузер (клиент antec-smoke)"
# Токен идёт только через конвейер: в argv, окружение и терминал он не попадает.
ids="$(echo '{}' | python3 "$COMPOSE_DIR/scripts/keycloak.py" user-token ${open_flag[@]+"${open_flag[@]}"} \
  | compose exec -T platform-api python -c "$(cat "$COMPOSE_DIR/scripts/platform_smoke.py")" \
      "$(cat "$ANTEC_INSTANCE_RENDERED/instance.json")")" \
  || die "проверка фасада не пройдена"

log "записи в БД platform и в учёте LiteLLM"
calls_sql="select json_build_object(
  'kind', k.kind, 'status', c.status, 'user_id', c.user_id,
  'prompt_tokens', c.prompt_tokens, 'completion_tokens', c.completion_tokens,
  'cost_usd', c.cost_usd, 'gateway_request_id', c.gateway_request_id,
  'roles', (select json_agg(m.role order by m.position) from messages m where m.model_call_id = c.id),
  'audit', (select json_agg(a.outcome) from audit_events a
            where a.request_id = c.id and a.action = 'model.call'))
from (values ('completion', :'completion'), ('stream', :'stream')) as k(kind, id)
join model_calls c on c.id = k.id::uuid"
calls="$(pg_exec psql -X -q -v ON_ERROR_STOP=1 -At -d platform \
  -v completion="$(json_field completion <<<"$ids")" -v stream="$(json_field stream <<<"$ids")" \
  <<<"$calls_sql")"
# Конечный пользователь вызова в LiteLLM (поле user): должен быть id пользователя Platform API,
# а не значение, которое прислал клиент.
end_users="$(python3 -c '
import json, sys
print("\n".join(json.loads(r)["gateway_request_id"] or "-" for r in sys.stdin.read().splitlines() if r.strip()))' <<<"$calls" \
  | while read -r gw; do
      # LiteLLM пишет расходы асинхронно (у потока заметно позже ответа): ждём до 30 с.
      for _ in $(seq 1 15); do
        end_user="$(pg_exec psql -X -q -At -d litellm -v gw="$gw" <<'SQL'
select coalesce(max("end_user"), '-') from "LiteLLM_SpendLogs" where request_id = :'gw';
SQL
)"
        [[ "$end_user" != "-" ]] && break
        sleep 2
      done
      echo "$end_user"
    done)"

python3 - "$calls" "$end_users" <<'PY'
import json
import sys

rows = [json.loads(r) for r in sys.argv[1].splitlines() if r.strip()]
end_users = sys.argv[2].splitlines()
by_kind = {r["kind"]: (r, end_users[i] if i < len(end_users) else "-") for i, r in enumerate(rows)}
checks = {}
for kind in ("completion", "stream"):
    r, end_user = by_kind.get(kind, ({}, "-"))
    checks[f"{kind}: model_calls ok, токены записаны"] = (
        r.get("status") == "ok"
        and (r.get("prompt_tokens") or 0) > 0
        and (r.get("completion_tokens") or 0) > 0
    )
    checks[f"{kind}: в истории вопрос и ответ"] = r.get("roles") == ["user", "assistant"]
    checks[f"{kind}: аудит model.call allow"] = r.get("audit") == ["allow"]
    checks[f"{kind}: конечный пользователь в LiteLLM = пользователь Platform API"] = (
        end_user == r.get("user_id")
    )
for name, ok in checks.items():
    mark = "ok" if ok else "FAIL"
    print(f"  [{mark}] {name}", file=sys.stderr)
for kind in ("completion", "stream"):
    r, _ = by_kind.get(kind, ({}, "-"))
    print(f"  {kind}: стоимость {r.get('cost_usd')} USD", file=sys.stderr)
sys.exit(0 if all(checks.values()) else 1)
PY

agent_id="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("agent", ""))' "$ids")"
if [[ -n "$agent_id" ]]; then
  log "запуски агента в БД platform (ADR-0010)"
  runs_sql="select json_build_object(
    'kind', k.kind, 'status', r.status, 'steps', r.steps, 'user_id', r.user_id,
    'prompt_tokens', r.prompt_tokens, 'completion_tokens', r.completion_tokens,
    'calls', (select json_agg(json_build_object('status', c.status, 'tokens', c.prompt_tokens,
                'gw', c.gateway_request_id)) from model_calls c where c.agent_run_id = r.id),
    'roles', (select json_agg(m.role order by m.position) from messages m
              join model_calls c on c.id = m.model_call_id where c.agent_run_id = r.id),
    'audit', (select json_agg(a.action || ':' || a.outcome order by a.occurred_at, a.id)
              from audit_events a where a.request_id = r.id))
  from (values ('agent', :'agent'), ('agent_stream', :'agent_stream')) as k(kind, id)
  join agent_runs r on r.id = k.id::uuid"
  runs="$(pg_exec psql -X -q -v ON_ERROR_STOP=1 -At -d platform \
    -v agent="$agent_id" -v agent_stream="$(json_field agent_stream <<<"$ids")" <<<"$runs_sql")"
  python3 - "$runs" <<'PY'
import json
import sys

rows = {r["kind"]: r for r in map(json.loads, filter(str.strip, sys.argv[1].splitlines()))}
checks = {}
for kind in ("agent", "agent_stream"):
    r = rows.get(kind, {})
    calls = r.get("calls") or []
    checks[f"{kind}: agent_runs ok, шаги и токены"] = (
        r.get("status") == "ok" and r.get("steps", 0) >= 1 and (r.get("prompt_tokens") or 0) > 0
    )
    checks[f"{kind}: шаги в model_calls с agent_run_id"] = len(calls) == r.get("steps") and all(
        c["status"] == "ok" and (c["tokens"] or 0) > 0 and c["gw"] for c in calls
    )
    checks[f"{kind}: в истории вопрос и ответ агента"] = r.get("roles") == ["user", "assistant"]
    audit = r.get("audit") or []
    checks[f"{kind}: аудит model.call по шагам и agent.run"] = (
        audit[-1:] == ["agent.run:allow"] and audit.count("model.call:allow") == r.get("steps")
    )
for name, ok in checks.items():
    print(f"  [{'ok' if ok else 'FAIL'}] {name}", file=sys.stderr)
sys.exit(0 if all(checks.values()) else 1)
PY
fi
log "Platform API: проверка пройдена"
