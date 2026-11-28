-- Агрегаты для Grafana (ADR-0013, п. 5). Выполняет scripts/up.sh суперпользователем, если
-- наблюдаемость включена; идемпотентно. Пароль роли platform_metrics up.sh задаёт строкой
-- \set pw `cat /run/antec-secrets/platform_metrics` перед скриптом: не в argv и не в журнале.
-- Роль видит только представления схемы metrics: без текстов, id пользователей, разговоров,
-- клиентов и ошибок; доступа к схемам public и audit нет. Представления принадлежат postgres и
-- читают таблицы от его имени.
SELECT format('CREATE ROLE platform_metrics LOGIN CONNECTION LIMIT 3 PASSWORD %L', :'pw')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_metrics') \gexec
ALTER ROLE platform_metrics LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  CONNECTION LIMIT 3 PASSWORD :'pw';
GRANT CONNECT ON DATABASE platform TO platform_metrics;

BEGIN;
CREATE SCHEMA IF NOT EXISTS metrics AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA metrics FROM PUBLIC;

-- Вызовы моделей по часу, модели и итогу; agent = шаг агента (иначе прямой вызов фасада).
CREATE OR REPLACE VIEW metrics.model_calls_hourly AS
SELECT date_trunc('hour', started_at) AS hour,
       model,
       status,
       agent_run_id IS NOT NULL AS agent,
       count(*) AS calls,
       coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
       coalesce(sum(completion_tokens), 0) AS completion_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd
  FROM public.model_calls
 GROUP BY 1, 2, 3, 4;

REVOKE ALL ON ALL TABLES IN SCHEMA metrics FROM PUBLIC;
GRANT USAGE ON SCHEMA metrics TO platform_metrics;
GRANT SELECT ON metrics.model_calls_hourly TO platform_metrics;
COMMIT;
