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

# Заметки mcp-demo до прогона: журнал контейнера копится между прогонами, считается прирост.
notes_before="$(compose logs mcp-demo 2>/dev/null | grep -c 'note created owner=' || true)"

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
tool_time="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("tool_time", ""))' "$ids")"
if [[ -n "$tool_time" ]]; then
  log "инструменты и подтверждения в БД platform и в mcp-demo (ADR-0010)"
  tools_sql="select json_build_object(
    'kind', k.kind,
    'run_status', (select r.status from agent_runs r where r.id = k.id::uuid),
    'parent', (select r.parent_run_id from agent_runs r where r.id = k.id::uuid),
    'audit', (select json_agg(a.action || ':' || a.outcome || ':' || coalesce(a.resource_id, '')
                order by a.occurred_at, a.id)
              from audit_events a where a.request_id = k.id::uuid
                and a.action in ('tool.call', 'tool.approval')))
  from json_each_text(:'ids'::json) as k(kind, id)
  where k.kind in ('tool_time', 'pause_deny', 'resume_deny', 'pause_ok', 'resume_ok',
                   'pause_api', 'resume_api', 'class_lab')"
  tools="$(pg_exec psql -X -q -v ON_ERROR_STOP=1 -At -d platform -v ids="$ids" <<<"$tools_sql")"
  notes_after="$(compose logs mcp-demo 2>/dev/null | grep -c 'note created owner=' || true)"
  notes=$(( ${notes_after:-0} - ${notes_before:-0} ))
  python3 - "$tools" "$notes" <<'PY'
import json
import sys

rows = {r["kind"]: r for r in map(json.loads, filter(str.strip, sys.argv[1].splitlines()))}
notes = int(sys.argv[2] or 0)
audit = {k: r.get("audit") or [] for k, r in rows.items()}
checks = {
    "tool.call get_time allow в аудите": "tool.call:allow:mcp-demo.get_time" in audit["tool_time"],
    "пауза: статус denied после «отмена»": rows["pause_deny"]["run_status"] == "denied",
    "«отмена»: tool.approval deny, инструмент не вызван": (
        any(a.startswith("tool.approval:deny") for a in audit["resume_deny"])
        and not any(a.startswith("tool.call") for a in audit["resume_deny"])
    ),
    "подтверждение: статус approved, продолжение ссылается на паузу": (
        rows["pause_ok"]["run_status"] == "approved" and rows["resume_ok"]["parent"] is not None
    ),
    "подтверждение: tool.approval allow и tool.call create_note allow": (
        any(a.startswith("tool.approval:allow") for a in audit["resume_ok"])
        and "tool.call:allow:mcp-demo.create_note" in audit["resume_ok"]
    ),
    "решение через API: tool.call create_note allow": (
        "tool.call:allow:mcp-demo.create_note" in audit["resume_api"]
    ),
    "классы данных: agent-lab create_note deny, вызова нет (ADR-0011)": (
        "tool.call:deny:mcp-demo.create_note" in audit.get("class_lab", [])
        and "tool.call:allow:mcp-demo.create_note" not in audit.get("class_lab", [])
    ),
    "mcp-demo: созданы 2 подтверждённые заметки (токен обмена принят)": notes >= 2,
}
for name, ok in checks.items():
    print(f"  [{'ok' if ok else 'FAIL'}] {name}", file=sys.stderr)
sys.exit(0 if all(checks.values()) else 1)
PY
fi
log "Platform API: проверка пройдена"
