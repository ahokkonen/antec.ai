-- Выгрузка для scripts/audit_chain.py: строка chain_head, затем события по порядку (JSON на
-- строку). Один снимок. details передаётся текстом: именно он входит в хэш.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object('chain_id', chain_id, 'seq', seq, 'hash', encode(hash, 'hex'),
  'base_seq', base_seq, 'base_hash', encode(base_hash, 'hex')) FROM audit.chain_head;
SELECT json_build_object('seq', seq, 'id', id,
  'occurred_us', trunc(extract(epoch FROM occurred_at) * 1000000)::bigint,
  'actor_user_id', actor_user_id, 'actor_client', actor_client, 'action', action,
  'resource_type', resource_type, 'resource_id', resource_id, 'outcome', outcome,
  'request_id', request_id, 'call_id', call_id, 'details', details::text,
  'prev_hash', encode(prev_hash, 'hex'), 'hash', encode(hash, 'hex'))
  FROM audit.audit_events ORDER BY seq;
COMMIT;
