-- Сверка аккаунтов Open WebUI с Keycloak (ADR-0018). Выполняется scripts/up.sh после запуска
-- Open WebUI (таблицы создают его миграции) от суперпользователя PostgreSQL в БД openwebui.
-- Переменные psql:
--   admins     JSON-массив antec_uid с ролью admin клиента open-webui в Keycloak
--   uid_map    JSON-объект: id пользователя Keycloak -> antec_uid
--   seed_id    id аккаунта breakglass, если Open WebUI пуст
--   seed_hash  bcrypt случайного одноразового пароля (вход по паролю выключен)
--   now        текущее время, секунды Unix
\set ON_ERROR_STOP on
BEGIN;

-- 1. Аккаунты, созданные по sub Keycloak (до OAUTH_SUB_CLAIM=antec_uid), переносятся на
--    antec_uid, иначе Open WebUI их не найдёт и откажет во входе из-за занятого email.
WITH moved AS (
  UPDATE "user" u
     SET oauth = json_build_object('oidc', json_build_object('sub', m.value))
    FROM json_each_text(:'uid_map'::json) m
   WHERE u.oauth -> 'oidc' ->> 'sub' = m.key
  RETURNING 1
)
SELECT 'перенесено на antec_uid: ' || count(*) FROM moved;

-- 2. Пустой Open WebUI получает аккаунт breakglass с ролью admin. Иначе администратором стал бы
--    первый вошедший: Open WebUI повышает единственного пользователя до admin в обход ролей.
WITH seeded AS (
  INSERT INTO "user" (id, name, email, role, profile_image_url,
                      last_active_at, updated_at, created_at, oauth)
  SELECT :'seed_id', 'Аварийный вход', 'breakglass@antec.invalid', 'admin', '/user.png',
         :now, :now, :now, json_build_object('oidc', json_build_object('sub', 'breakglass'))
   WHERE NOT EXISTS (SELECT 1 FROM "user")
  RETURNING id
)
INSERT INTO auth (id, email, password, active)
SELECT id, 'breakglass@antec.invalid', :'seed_hash', true FROM seeded;

-- 3. Роль admin в Open WebUI только у тех, у кого есть роль admin клиента open-webui в Keycloak.
WITH demoted AS (
  UPDATE "user"
     SET role = 'user'
   WHERE role = 'admin'
     AND coalesce(oauth -> 'oidc' ->> 'sub', '') NOT IN
         (SELECT json_array_elements_text(:'admins'::json))
  RETURNING 1
)
SELECT 'снята роль admin: ' || count(*) FROM demoted;

COMMIT;
