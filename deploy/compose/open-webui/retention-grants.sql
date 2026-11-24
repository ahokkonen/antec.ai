-- Права ролей Platform API в БД Open WebUI 0.13.3 (ADR-0012, пп. 1 и 7): удаление чатов по сроку
-- (platform_worker, без чтения текстов) и выгрузка с удалением по запросу субъекта
-- (platform_subject). Выполняет scripts/up.sh суперпользователем после миграций Open WebUI.
-- chat_message, chat_file и shared_chat удаляет каскад с правами владельца таблиц.
\set ON_ERROR_STOP on
BEGIN;
GRANT USAGE ON SCHEMA public TO platform_worker, platform_subject;
GRANT SELECT (id, user_id, created_at, updated_at), DELETE ON chat TO platform_worker, platform_subject;
GRANT SELECT (title, chat, summary) ON chat TO platform_subject;
GRANT SELECT (id, oauth) ON "user" TO platform_subject;
GRANT SELECT (chat_id), UPDATE (chat_id) ON automation_run TO platform_worker, platform_subject;
GRANT SELECT (id, resource_type, resource_id), DELETE ON comment TO platform_worker, platform_subject;
GRANT SELECT (comment_id), DELETE ON comment_reaction TO platform_worker, platform_subject;
GRANT SELECT (resource_type, resource_id), DELETE ON notification TO platform_worker, platform_subject;
GRANT SELECT (chat_id), DELETE ON chat_view_state TO platform_worker, platform_subject;
GRANT SELECT (chat_id), DELETE ON chatidtag TO platform_worker, platform_subject;
COMMIT;
