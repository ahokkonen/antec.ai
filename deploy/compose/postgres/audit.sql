-- Схема аудита (ADR-0012, п. 4). Выполняет scripts/up.sh суперпользователем после миграций
-- platform; идемпотентно. Владелец platform_audit без входа: ни владелец БД platform, ни роли
-- сервисов не могут изменить таблицу, триггеры и функции.
--
-- Цепочка: hash = sha256(prev_hash || поля), см. audit.event_bytes. Порядок вставок задаёт
-- блокировка строки audit.chain_head до конца транзакции, поэтому приложение вставляет аудит
-- последним в транзакции. Проверка независима от БД: scripts/audit_chain.py.
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_audit') THEN
    CREATE ROLE platform_audit NOLOGIN;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION platform_audit;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

-- Перенос таблицы, созданной миграциями 0001-0006.
DO $$
BEGIN
  IF to_regclass('public.audit_events') IS NOT NULL THEN
    IF to_regclass('audit.audit_events') IS NOT NULL THEN
      RAISE EXCEPTION 'public.audit_events существует рядом с audit.audit_events: подмена аудита?';
    END IF;
    LOCK TABLE public.audit_events IN ACCESS EXCLUSIVE MODE;
    DROP TRIGGER IF EXISTS audit_events_no_update_delete ON public.audit_events;
    DROP TRIGGER IF EXISTS audit_events_no_truncate ON public.audit_events;
    ALTER TABLE public.audit_events SET SCHEMA audit;
    ALTER TABLE audit.audit_events OWNER TO platform_audit;
    -- Прежний триггер ссылался на функцию владельца БД: он мог бы подменить её тело.
    DROP FUNCTION IF EXISTS public.audit_events_append_only();
  END IF;
END
$$;

ALTER TABLE audit.audit_events ADD COLUMN IF NOT EXISTS seq bigint;
ALTER TABLE audit.audit_events ADD COLUMN IF NOT EXISTS prev_hash bytea;
ALTER TABLE audit.audit_events ADD COLUMN IF NOT EXISTS hash bytea;

CREATE TABLE IF NOT EXISTS audit.chain_head (
  only_row    boolean PRIMARY KEY DEFAULT true CHECK (only_row),
  chain_id    uuid NOT NULL DEFAULT gen_random_uuid(),
  seq         bigint NOT NULL,
  hash        bytea NOT NULL,
  occurred_at timestamptz NOT NULL,
  -- Последнее удалённое по сроку событие: с него начинается проверка.
  base_seq    bigint NOT NULL DEFAULT 0,
  base_hash   bytea NOT NULL DEFAULT '\x'
);
ALTER TABLE audit.chain_head OWNER TO platform_audit;

-- Поле: длина в байтах, двоеточие, значение; NULL = «-». Время в микросекундах Unix.
CREATE OR REPLACE FUNCTION audit.event_bytes(
  p_prev bytea, p_seq bigint, p_id uuid, p_occurred_at timestamptz, p_actor_user_id uuid,
  p_actor_client text, p_action text, p_resource_type text, p_resource_id text, p_outcome text,
  p_request_id uuid, p_call_id uuid, p_details jsonb
) RETURNS bytea LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  out text := encode(p_prev, 'hex');
  item text;
BEGIN
  FOREACH item IN ARRAY ARRAY[
    p_seq::text, p_id::text, trunc(extract(epoch FROM p_occurred_at) * 1000000)::bigint::text,
    p_actor_user_id::text, p_actor_client, p_action, p_resource_type, p_resource_id, p_outcome,
    p_request_id::text, p_call_id::text, p_details::text
  ] LOOP
    out := out || ';' ||
      CASE WHEN item IS NULL THEN '-' ELSE octet_length(convert_to(item, 'UTF8')) || ':' || item END;
  END LOOP;
  RETURN convert_to(out, 'UTF8');
END
$$;

CREATE OR REPLACE FUNCTION audit.chain_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  head audit.chain_head;
BEGIN
  SELECT * INTO STRICT head FROM audit.chain_head FOR UPDATE;
  -- Номер, время и хэши задаёт БД, а не вызывающий; время не убывает вместе с номером.
  NEW.seq := head.seq + 1;
  NEW.occurred_at := greatest(clock_timestamp(), head.occurred_at);
  NEW.prev_hash := head.hash;
  NEW.hash := sha256(audit.event_bytes(
    NEW.prev_hash, NEW.seq, NEW.id, NEW.occurred_at, NEW.actor_user_id, NEW.actor_client,
    NEW.action, NEW.resource_type, NEW.resource_id, NEW.outcome, NEW.request_id, NEW.call_id,
    NEW.details));
  UPDATE audit.chain_head
     SET seq = NEW.seq, hash = NEW.hash, occurred_at = NEW.occurred_at;
  RETURN NEW;
END
$$;

-- Удаление только из audit.purge (владелец схемы) и не моложе 180 дней.
CREATE OR REPLACE FUNCTION audit.append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_user = 'platform_audit'
     AND OLD.occurred_at < now() - interval '180 days' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'audit_events: только добавление (ADR-0012)'
    USING ERRCODE = 'insufficient_privilege';
END
$$;

-- Начало цепочки старше p_days, не больше p_limit событий за вызов.
CREATE OR REPLACE FUNCTION audit.purge(p_days integer, p_limit integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  last_seq bigint;
  deleted integer;
BEGIN
  IF p_days IS NULL OR p_days < 180 THEN
    RAISE EXCEPTION 'срок аудита не меньше 180 дней (ADR-0012)';
  END IF;
  PERFORM 1 FROM audit.chain_head FOR UPDATE;
  SELECT max(s.seq) INTO last_seq FROM (
    SELECT e.seq, e.occurred_at FROM audit.audit_events e
     ORDER BY e.seq LIMIT least(greatest(coalesce(p_limit, 1000), 1), 10000)
  ) s WHERE s.occurred_at < now() - make_interval(days => p_days);
  IF last_seq IS NULL THEN
    RETURN 0;
  END IF;
  UPDATE audit.chain_head
     SET base_seq = last_seq,
         base_hash = (SELECT e.hash FROM audit.audit_events e WHERE e.seq = last_seq);
  DELETE FROM audit.audit_events WHERE seq <= last_seq;
  GET DIAGNOSTICS deleted = ROW_COUNT;
  RETURN deleted;
END
$$;

-- События до цепочки: номера и хэши по времени (один раз).
DO $$
DECLARE
  ev audit.audit_events;
  n bigint := 0;
  prev bytea := '\x';
  last_at timestamptz := '-infinity';
BEGIN
  IF EXISTS (SELECT 1 FROM audit.chain_head) THEN
    RETURN;
  END IF;
  FOR ev IN SELECT * FROM audit.audit_events ORDER BY occurred_at, id LOOP
    n := n + 1;
    UPDATE audit.audit_events SET seq = n, prev_hash = prev,
      hash = sha256(audit.event_bytes(prev, n, ev.id, ev.occurred_at, ev.actor_user_id,
        ev.actor_client, ev.action, ev.resource_type, ev.resource_id, ev.outcome,
        ev.request_id, ev.call_id, ev.details))
     WHERE id = ev.id RETURNING hash INTO prev;
    last_at := ev.occurred_at;
  END LOOP;
  INSERT INTO audit.chain_head (seq, hash, occurred_at) VALUES (n, prev, last_at);
END
$$;

ALTER TABLE audit.audit_events ALTER COLUMN seq SET NOT NULL;
ALTER TABLE audit.audit_events ALTER COLUMN prev_hash SET NOT NULL;
ALTER TABLE audit.audit_events ALTER COLUMN hash SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_events_seq ON audit.audit_events (seq);

-- ENABLE ALWAYS: триггеры действуют и при session_replication_role = replica.
CREATE OR REPLACE TRIGGER audit_events_chain BEFORE INSERT ON audit.audit_events
  FOR EACH ROW EXECUTE FUNCTION audit.chain_event();
CREATE OR REPLACE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE
  ON audit.audit_events FOR EACH ROW EXECUTE FUNCTION audit.append_only();
CREATE OR REPLACE TRIGGER audit_events_no_truncate BEFORE TRUNCATE
  ON audit.audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit.append_only();
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_chain;
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_no_update_delete;
ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER audit_events_no_truncate;

ALTER FUNCTION audit.event_bytes(bytea, bigint, uuid, timestamptz, uuid, text, text, text, text,
  text, uuid, uuid, jsonb) OWNER TO platform_audit;
ALTER FUNCTION audit.chain_event() OWNER TO platform_audit;
ALTER FUNCTION audit.append_only() OWNER TO platform_audit;
ALTER FUNCTION audit.purge(integer, integer) OWNER TO platform_audit;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM PUBLIC, platform, platform_app, platform_worker,
  platform_subject;
GRANT USAGE ON SCHEMA audit TO platform_app, platform_worker, platform_subject;
GRANT SELECT, INSERT ON audit.audit_events TO platform_app, platform_worker, platform_subject;
GRANT EXECUTE ON FUNCTION audit.purge(integer, integer) TO platform_worker;
COMMIT;
