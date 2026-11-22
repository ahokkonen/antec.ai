# ADR-0006: Модель данных и границы Platform API: пользователи, история, аудит, фасад

- Статус: предложено
- Дата: 2026-11-22
- Ответственный: CTO
- Связанные: ADR-0001, ADR-0002, ADR-0003, ADR-0004, ADR-0005, ADR-0007, ADR-0009 (в очереди),
  ADR-0012 (в очереди), ADR-0017, ADR-0018, [architecture/overview.md](../architecture/overview.md),
  [policy/allowed-versions.yaml](../../policy/allowed-versions.yaml)

## Контекст

Platform API владеет всем, что нельзя потерять при замене UI, шлюза или IdP: пользователями,
историей, аудитом, расходами (overview.md). Его ещё нет: Open WebUI ходит в LiteLLM напрямую
своим ключом, это временное отклонение от ADR-0002, п. 1 (ADR-0018, п. 7). БД `platform` и роль
`platform` уже созданы базовым слоем (ADR-0017), pgvector включён только в ней.

Факты (проверено 2026-11-22):

- Open WebUI 0.13.3 умеет передавать в OpenAI-совместимое подключение токен пользователя:
  `auth_type: "system_oauth"` в `OPENAI_API_CONFIGS` кладёт в `Authorization` access token из
  OAuth-сессии пользователя (`backend/open_webui/utils/headers.py`, тег v0.13.3). Токен
  обновляется refresh-токеном, если до истечения меньше 5 минут, под блокировкой на сессию
  (`utils/oauth.py`, `OAuthManager._refresh_token`). Если обновить не удалось, OAuth-сессия
  удаляется и запрос уходит без токена. Список моделей тоже запрашивается от имени пользователя.
- Идентификатор чата Open WebUI передаёт заголовком `X-OpenWebUI-Chat-Id` только при
  `ENABLE_FORWARD_USER_INFO_HEADERS`; вместе с ним идут заголовки `X-OpenWebUI-User-*`, которые
  ничем не подписаны.
- Токены клиента `open-webui` уже содержат `aud: platform-api` (client scope `platform-api`,
  realm как код), `azp: open-webui`, `sub`, `idp`, `entra_oid`, `entra_tid` (ADR-0007). Срок
  access token в realm 5 минут, SSO idle 30 минут, сессия Open WebUI 8 часов.
- LiteLLM принимает от клиента не только параметры OpenAI, но и свои (`api_base`, `api_key`,
  `metadata`, `mock_response` и др.); идентификатор конечного пользователя (`user`) он не
  проверяет (ADR-0018, п. 6).
- Python 3.14 (образ `python:3.14.10-slim-trixie`) и uv 0.13.8 в реестре; `uuid.uuid7()` есть в
  стандартной библиотеке с 3.14. Зависимости (лицензия, версия вне карантина 7 дней, OSV пусто):
  FastAPI 0.163.0 (MIT), uvicorn 0.54.0 (BSD-3-Clause), Pydantic 2.14.3 и pydantic-settings
  2.16.3 (MIT), SQLAlchemy 2.1.8 и Alembic 1.20.1 (MIT), asyncpg 0.32.0 (Apache-2.0), PyJWT
  2.15.1 (MIT), httpx 0.28.1 (BSD-3-Clause), cryptography 50.0.2 (Apache-2.0 OR BSD-3-Clause).
  psycopg 3 распространяется под LGPL-3.0 и не используется.

## Варианты

Аутентификация Open WebUI в Platform API:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Токен пользователя Keycloak (`system_oauth`)** | Пользователь доказан подписью IdP: `(iss, sub)` и claims Entra из токена; тот же путь для любых клиентов API; токен годится для обмена на токен MCP-сервера (ADR-0007, п. 3) | Зависит от OAuth-сессии Open WebUI: после истечения SSO-сессии Keycloak нужен повторный вход |
| Служебный ключ Open WebUI + подписанный HS256 JWT пользователя (`FORWARD_USER_INFO_HEADER_JWT`) | Не зависит от сессии Keycloak | Общий секрет; в JWT только id Open WebUI, без `(iss, sub)`; компрометация Open WebUI = доступ от имени любого пользователя |
| Служебный ключ + заголовки `X-OpenWebUI-User-*` | Просто | Пользователя утверждает вызывающий; то же, что сейчас в LiteLLM |

Модель пользователей:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **`users` + `identities(iss, sub)`, создание при первом валидном токене** | Несколько идентичностей на пользователя (замена IdP, SCIM); доступ всё равно ограничивает IdP («Assignment required» в Entra) | Пользователь появляется в Platform API только после первого запроса |
| Только заранее заведённые пользователи | Явный контроль | Ручная работа до SCIM; дублирует назначение в Entra |

Организации:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Нет таблицы: одна инсталляция = одна организация (ADR-0005)** | Проще схема и запросы; изоляция клиентов инсталляциями, а не строками | Несколько организаций в одной инсталляции потребуют миграции |
| Таблица с одной строкой и `org_id` везде | Готово к нескольким организациям | Сложность без пользы в v1 |

История:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Текст сообщений хранится, срок 180 дней (Допущение)** | Источник истины истории у нас (ADR-0002); выгрузка при замене UI | Персональные данные в нашей БД: нужны срок хранения и очистка (ADR-0012, ADR-0015) |
| Только метаданные до ADR-0012 и DPIA | Меньше персональных данных | История остаётся только в Open WebUI, противоречит overview.md |

## Решение

1. **Граница.** Platform API единственный владелец данных пользователей, истории, аудита и
   расходов; Open WebUI и другие клиенты ходят только в его OpenAI-совместимый фасад, Platform API
   в LiteLLM служебным ключом. Код: `services/platform-api/` (отдельный uv-проект, пакет
   `antec_platform`), образ `localhost/antec/platform-api:<версия>` от `python` и `uv` из реестра.
   Протоколы: OpenAI Chat Completions, OIDC/JWKS, SQL. SDK вендоров не используются: LiteLLM
   вызывается по HTTP через адаптер шлюза (`antec_platform/adapters/litellm.py`, интерфейс
   `ChatGateway`).
2. **Фасад v1:** `GET /v1/models`, `POST /v1/chat/completions` (с потоком SSE и без), `GET /healthz`
   (процесс жив), `GET /readyz` (БД и JWKS доступны). Модели: только логические имена из
   конфигурации (`chat-default`, `chat-fast`), неизвестная модель = 404 `model_not_found`.
   В LiteLLM уходят только параметры из белого списка OpenAI (`messages`, `stream`,
   `stream_options`, `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`,
   `presence_penalty`, `frequency_penalty`, `seed`, `response_format`, `tools`, `tool_choice`,
   `parallel_tool_calls`, `reasoning_effort`); остальные отбрасываются. Поле `user` всегда
   заменяется идентификатором пользователя Platform API. Ошибки в формате OpenAI
   (`{"error": {"message", "type", "code"}}`). Агенты, RAG, MCP, Cerbos (ADR-0009), гардрейлы и
   OTel добавляются следующими шагами.
3. **Аутентификация:** `Authorization: Bearer <access token Keycloak>`. Проверяются подпись (только
   RS256, ключи из JWKS realm с кэшем и обновлением при неизвестном `kid`), `iss` = issuer realm,
   `exp` и `nbf` (допуск 30 с), `aud` содержит `platform-api`, `azp` из списка разрешённых
   клиентов (`open-webui`; на стенде ещё `antec-smoke`). Токены с `typ` не `Bearer` отклоняются.
   Open WebUI передаёт токен через `auth_type: "system_oauth"`; заголовки `X-OpenWebUI-User-*`
   Platform API игнорирует, `X-OpenWebUI-Chat-Id` использует только как внешний идентификатор
   разговора внутри пользователя. Срок access token клиента `open-webui` 15 минут: при 5 минутах
   Open WebUI обновлял бы токен перед каждым запросом.
4. **Пользователи.** `users` (`id` UUIDv7, `display_name`, `email`, `status` active или disabled,
   `created_at`, `last_seen_at`) и `identities` (`user_id`, `issuer`, `subject`, `idp`, `entra_tid`,
   `entra_oid`, `created_at`, `last_seen_at`), уникальны `(issuer, subject)` и `(entra_tid,
   entra_oid)`. Первый валидный токен создаёт пользователя и идентичность. Если `(iss, sub)`
   неизвестен, а `(entra_tid, entra_oid)` уже есть (realm пересоздан, `sub` сменился),
   идентичность переносится на новый `sub` с записью в аудит; у локальных пользователей Keycloak
   такой привязки нет, после пересоздания realm они становятся новыми пользователями.
   Пользователь со статусом disabled получает 403. Имя и email обновляются из токена.
5. **Организации:** отдельной таблицы нет, одна инсталляция = одна организация (ADR-0005).
6. **История.** `conversations` (`user_id`, `client_id` = `azp`, `external_id` = идентификатор чата
   клиента, уникальны в пределах пользователя и клиента; без идентификатора каждый запрос свой
   разговор) и `messages` (`conversation_id`, `model_call_id`, `role`, `content` JSONB,
   `created_at`). Клиент OpenAI API присылает всю историю в каждом запросе, поэтому сохраняются
   только новые сообщения: идущие после последнего ответа ассистента, и сам ответ. Срок хранения
   180 дней (Допущение), задача очистки и окончательный срок в ADR-0012.
7. **Вызовы моделей и расходы.** `model_calls` (id = идентификатор запроса, `user_id`,
   `conversation_id`, `client_id`, `model`, `stream`, `status` ok, error, cancelled, rejected,
   `http_status`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `gateway_request_id`,
   `started_at`, `finished_at`). Токены берутся из `usage` ответа (для потока Platform API
   запрашивает `stream_options.include_usage`), стоимость из ответа шлюза, если он её сообщает
   (LiteLLM: только без потока), иначе пусто до колбэка расходов (ADR-0001, п. 4).
   `gateway_request_id` = `id` ответа: по нему LiteLLM записывает расход (SpendLogs). Источник истины расходов `model_calls`.
   Бюджеты на пользователя считает LiteLLM по `user` = id пользователя Platform API: ключ
   `platform-api` только с логическими моделями и бюджетом конечного пользователя
   `platform-user` (5 USD на 30 дней, как у Open WebUI в ADR-0018).
8. **Аудит.** `audit_events` (UUIDv7, `occurred_at`, `actor_user_id`, `actor_client`, `action`,
   `resource_type`, `resource_id`, `outcome` allow, deny, error, `request_id`, `details` JSONB без
   текстов сообщений и токенов). Пишутся: каждый вызов модели (`model.call`), создание
   пользователя, перенос идентичности, отказ по валидному токену (чужой `aud` или `azp`,
   disabled). Невалидные токены (подпись, срок) только в лог: иначе аудит можно засорить
   анонимно. Таблица только на добавление: у роли сервиса `INSERT` и `SELECT`, триггер запрещает
   `UPDATE`, `DELETE` и `TRUNCATE`. Аудит вызова пишется в той же транзакции, что и `model_calls`.
   Цепочка хэшей, вынос аудита в отдельное хранилище и сроки в ADR-0012.
9. **Роли PostgreSQL.** `platform` (владелец схемы) только для миграций; сервис работает ролью
   `platform_app` без DDL: `SELECT`, `INSERT`, `UPDATE` на рабочие таблицы, `SELECT` и `INSERT` на
   `audit_events`, `DELETE` нет ни у кого, кроме будущей задачи очистки. Миграции Alembic
   выполняет одноразовый сервис `platform-migrate` до запуска `platform-api`. Доступ к данным:
   SQLAlchemy 2.1 Core + asyncpg.
10. **Секреты.** `antec/platform-migrate` (пароль `platform`) и `antec/platform-api` (пароль
    `platform_app`, ключ LiteLLM `platform-api`) в OpenBao, каждый выгружается своим токеном
    `secrets-render` в свой том; сервис читает файлы (`secrets_dir` pydantic-settings), в
    окружении и argv секретов нет.
11. **Сеть стенда:** `platform-api` в сетях `data` (PostgreSQL, JWKS Keycloak), `llm` (LiteLLM) и
    новой `platform` (internal, Open WebUI -> Platform API). Open WebUI выходит из сети `llm`;
    его ключ в LiteLLM отзывается. Порт Platform API на хост не публикуется.
12. **Образ:** пользователь без root, корневая ФС только для чтения, `cap_drop: ALL`, зависимости
    только `uv sync --locked` с хэшами, `exclude-newer` = 7 дней до обновления lock (ADR-0016,
    п. 6); пакет `litellm` запрещён в `uv.lock` (`python_forbidden`).

## Последствия

- Плюс: снимается отклонение ADR-0018, п. 7: Open WebUI больше не ходит в LiteLLM и не
  утверждает пользователя сам; пользователь в Platform API доказан подписью IdP.
- Плюс: история, расходы и аудит в нашей БД, UI и шлюз заменяемы без миграции данных.
- Плюс: обмен токена пользователя на токен MCP-сервера (ADR-0007) возможен без изменения входа.
- Минус: когда SSO-сессия Keycloak истекает (30 минут без активности), Open WebUI не может
  обновить токен и фасад отвечает 401, хотя сессия Open WebUI (8 часов) ещё жива: пользователь
  входит заново.
- Минус: текст сообщений хранится дважды (Open WebUI и Platform API); персональные данные в
  БД `platform` до решения о сроках (ADR-0012, ADR-0015).
- Минус: владелец схемы может отключить триггер аудита; защита от администратора БД в ADR-0012.
- Минус: стоимость потоковых ответов может быть пустой до колбэка расходов.
- Минус: служебные генерации Open WebUI (заголовок чата) приходят с тем же идентификатором чата и
  сохраняются в истории разговора как обычные сообщения; отделить их надёжно по запросу нельзя.
  Решается при выгрузке истории или отдельным признаком клиента.

## План выхода (как заменить)

Клиенты знают только OpenAI-совместимый API и OIDC; данные в PostgreSQL в документированной
схеме (миграции Alembic в репозитории). Смена IdP: новая идентичность в `identities` по
`entra_oid` или SCIM. Смена шлюза: новый адаптер `ChatGateway`. Смена UI: другой клиент того же
фасада, история выгружается SQL.

## Допущения и открытые вопросы

- Допущение: срок хранения истории 180 дней; окончательно в ADR-0012 после DPIA (ADR-0015).
- Допущение: OAuth-сессия Open WebUI переживает перезапуск Open WebUI (хранится в его БД);
  проверить на стенде.
- [ ] Согласовать сессию Open WebUI с SSO-сессией Keycloak (короче `JWT_EXPIRES_IN` или
      offline-токены) после опыта пилота.
- [ ] Колбэк расходов LiteLLM в `model_calls` (ADR-0001, п. 4).
- [ ] Клиенты API кроме Open WebUI и их права: вместе с Cerbos (ADR-0009).
- [ ] Очистка истории по сроку и роль для неё (ADR-0012).
- [ ] Сеть `data` на стенде общая: Open WebUI достижим LiteLLM и наоборот, доступ закрывают
      ключи и токены. Отдельные сети к PostgreSQL на сервис или egress-политики на сервере.

## Ссылки

- https://github.com/open-webui/open-webui/blob/v0.13.3/backend/open_webui/utils/headers.py
- https://github.com/open-webui/open-webui/blob/v0.13.3/backend/open_webui/utils/oauth.py
- https://platform.openai.com/docs/api-reference/chat
- https://docs.litellm.ai/docs/proxy/customers
- https://www.rfc-editor.org/rfc/rfc9068
- https://docs.python.org/3.14/library/uuid.html
