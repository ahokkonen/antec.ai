# ADR-0007: IdP и делегирование прав агенту: Keycloak с брокером Entra ID, token exchange, доступ к Graph

- Статус: предложено
- Дата: 2026-11-21
- Ответственный: CTO
- Связанные: ADR-0003, ADR-0004, ADR-0005, ADR-0006 (модель данных, в очереди), ADR-0009, ADR-0017,
  [policy/allowed-versions.yaml](../../policy/allowed-versions.yaml), [deploy/compose](../../deploy/compose/README.md)

## Контекст

brief.md требует собственный OIDC-провайдер с федерацией с любым внешним IdP; requirements.md
называет Entra ID для v1 и SCIM позже. Агент действует от имени пользователя и не получает больше
его прав (AGENTS.md, правило 5). MCP-сервер M365 ходит в Microsoft Graph с делегированными правами.

Ограничения и факты (проверено 2026-11-21 по документации Keycloak 26.8 и Microsoft):

- Mtech хостит отдельные инсталляции для клиентов (ADR-0005), у клиентов разные IdP
  (Entra ID, Google, другие OIDC/SAML). Open WebUI, LiteLLM и Platform API должны знать одного issuer.
- Keycloak 26.8.2 в реестре (Apache-2.0). Образ не подписан cosign; BuildKit provenance указывает
  на коммит тега 26.8.2; тег пересобран 2026-11-19 (правило 7 ADR-0017).
- Token exchange в Keycloak 26.8: стандартный V2 (RFC 8693) полностью поддерживается, но только
  internal-to-internal и только для confidential-клиентов; параметр `resource` не поддерживается.
  Делегирование с claim `act` (`token-exchange-delegation`) в статусе preview. Legacy V1
  (external-to-internal, `requested_issuer`) preview и deprecated.
- Получить токен внешнего IdP (Entra) через V2 нельзя; Keycloak предлагает Identity Brokering API
  V2 (supported с 26.7, по умолчанию выключен, есть «Store token in session»). V1 этого API устарел.
- Entra OBO: входящий токен должен иметь `aud` самого приложения, которое делает OBO
  («Applications can't redeem a token for a different app»). Токен, выданный Keycloak, для OBO
  не годится.
- С 26.8.2 Admin API принимает только токены с аудиторией Admin API (scope `kc-admin-api`).
- Ресурсы: документация рекомендует контейнеру до 2 GB; на стенде Keycloak занимает около 0,7 GB
  при лимите 1,5 GB. VM Docker 7,7 GB, полный MVP около 17 GB (ADR-0017).
- Команда DevOps начинающая: каждый компонент должен настраиваться кодом и проверяться скриптом.

## Варианты

| Вариант | Версия, лицензия | Плюсы | Минусы | Стоимость поддержки |
| --- | --- | --- | --- | --- |
| **Keycloak как собственный OIDC-провайдер, брокер в Entra ID** | 26.8.2, Apache-2.0 | Один issuer для всех сервисов; новый IdP клиента = брокер в realm, без изменений кода; аварийный вход без Entra; token exchange V2 между нашими сервисами; MFA и политики входа в одном месте | Ещё один сервис (0,7-1,5 GB RAM, БД, обновления); OBO в Graph невозможен, нужен брокерный токен; админ-консоль соблазняет менять realm руками | Средняя |
| Entra ID напрямую, Platform API хранит пользователей по `(iss, sub)` + `oid` | облачный сервис | Нет своего IdP; OBO в Graph из коробки; Conditional Access Entra | Каждый сервис должен поддерживать несколько issuer; Open WebUI умеет одного OIDC-провайдера; клиент с другим IdP требует изменений; нет аварийного входа; противоречит brief.md (нет обязательной зависимости от вендора) | Низкая для Mtech, высокая для клиентов |
| Authentik / Zitadel | MIT + Enterprise / AGPL-3.0 | Проще UI | Agent accounts и DCR в Authentik платные; Zitadel AGPL (docs/research/2026-11-21-component-scan.md) | Средняя, лицензионный риск |

Доступ к Graph при Keycloak:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Identity Brokering API V2 со «Store token in session»** | Один вход пользователя; токен Entra не хранится в БД Keycloak | Нужно проверить обновление токена и scopes Graph в PoC; функция выключена по умолчанию |
| Отдельное согласие OAuth в MCP-сервере, refresh token в OpenBao | Не зависит от IdP пользователя; стандарт MCP authorization | Второе окно согласия; свой код хранения и ротации токенов |
| OBO | Стандарт Microsoft | Невозможно: токен Keycloak не принимается как assertion |

Делегирование агенту:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Standard token exchange V2 с сужением `aud`** | Supported; токен на каждый MCP-сервер своей аудиторией; срок не больше исходного (26.8.2) | Нет `act` в токене: кто действует, пишет наш аудит |
| Token exchange delegation (`act`, `may_act`) | Актор в токене по RFC 8693 | Preview; требует FGAP v2 и `parameterized-scopes` |

Realm как код:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **JSON realm + создание через Admin API скриптом стенда, если realm нет** | Без новых зависимостей; секреты не попадают в JSON и окружение; пересоздание одной командой | Дрейф в существующем realm не исправляется |
| `--import-realm` при старте | Встроено | Плейсхолдеры только из переменных окружения, а секреты в окружении запрещены |
| keycloak-config-cli / Terraform-провайдер | Сверка с дрейфом | Новый компонент: проверка лицензии, версии, карантин; отдельное решение |

## Решение

1. **Keycloak 26.8.2 как собственный OIDC-провайдер** инсталляции, внешний IdP организации
   подключается брокером. Для Mtech: брокер `entra` (OIDC v2, PKCE S256, `trustEmail=false`,
   userinfo выключен, claims из ID-токена). Один realm на инсталляцию (`antec`), realm `master`
   только для администрирования.
2. **Идентичность в Platform API** хранится по `(iss, sub)` токена Keycloak; дополнительно
   claims `idp` (алиас брокера из сессии), `entra_oid`, `entra_tid` (атрибуты пользователя, которые
   пишет брокер, видны и меняются только администратором). Точная схема в ADR-0006. Это же
   готовит SCIM и замену Keycloak.
3. **Делегирование агенту: standard token exchange V2.** Platform API (confidential-клиент
   `platform-api`) обменивает токен пользователя на токен для конкретного MCP-сервера: `audience`
   и `scope` = имя сервера (например `mcp-m365`); допустимые аудитории задаются optional client
   scopes клиента `platform-api`, обмен на другие аудитории отклоняется. `sub` сохраняется, срок не
   больше исходного. Кто действует (агент, запуск, инструмент), записывает аудит Platform API.
   Preview-функции Keycloak (в том числе delegation) не включаются, пока не станут supported.
4. **Токены Graph:** Identity Brokering API V2 с «Store token in session» через адаптер в
   `mcp-m365`; проверяется в PoC на шаге MCP M365 (обновление токена, scopes Graph, отзыв). Если
   PoC не пройдёт, запасной вариант: отдельное согласие OAuth в MCP-сервере, refresh token в
   OpenBao. OBO не используется. До решения на шаге MCP M365 функция `identity-brokering-api`
   выключена при сборке образа.
5. **Аварийный вход:** локальные пользователи без федерации, пароли генерируются в OpenBao
   (`antec/keycloak`): `breakglass` в realm `antec` и `antec-admin` в realm `master`. OTP
   обязателен при каждом входе по паролю в обоих realm: browser flow `antec browser` (копия
   стандартного, шаг 2FA и OTP Form обязательны). Удаление OTP через account console не даёт
   входа по одному паролю: Keycloak требует настроить OTP заново. Вход через брокер этот шаг не
   проходит, MFA обеспечивает внешний IdP. Временный bootstrap-клиент удаляется после первой настройки,
   автоматизация работает через служебный клиент `antec-automation` (без full scope, с
   `kc-admin-api`).
6. **Realm как код:** `deploy/compose/keycloak/realm-antec.json` и `idp-entra.json`;
   `scripts/keycloak.py apply` создаёт через Admin API realm и недостающие объекты (scopes,
   клиенты, breakglass), не меняя существующие, и приводит к заданному состоянию секреты клиентов,
   профиль пользователя, flow входа и брокер. Прерванная первичная настройка достраивается
   повторным запуском. Изменения структуры realm на стенде
   применяются пересозданием (`up.sh --recreate-realm`); сверка с дрейфом на сервере решается до
   первой серверной инсталляции.
7. **Секреты:** пароль БД и секрет bootstrap-клиента в `keycloak.conf`, секрет клиента Entra в
   файловом vault Keycloak (`${vault.entra-client-secret}`); оба выгружает `secrets-render`
   токеном `render-keycloak`, который читает только `antec/keycloak` и `antec/entra`. Скрипты
   отличают отсутствие записи в OpenBao от ошибки чтения: при ошибке ничего не генерируется и
   не перезаписывается, прежние файлы секретов остаются. Пароль
   роли БД копируется в `antec/keycloak` скриптом `up.sh`. В переменных окружения и argv
   секретов нет.
8. **Образ:** локальная оптимизированная сборка `localhost/antec/keycloak:26.8.2` от образа по
   digest (`db=postgres`, `vault=file`, health); выключены `impersonation`, `identity-brokering-api`,
   `twitter-broker`. Запуск `start --optimized`, корневая ФС только для чтения, `cap_drop: ALL`.
9. **Сеть стенда:** Keycloak в сетях `data` и `egress` (выход к `login.microsoftonline.com`),
   порт 8080 только на `127.0.0.1` хоста, адрес `http://localhost:8080`; порт управления 9000
   наружу не публикуется. На сервере TLS через reverse proxy и отдельный адрес админ-консоли.
10. **Приложение в Entra ID** (одно на инсталляцию): single-tenant, Web redirect URI
    `<адрес Keycloak>/realms/antec/broker/entra/endpoint`, client secret со сроком не больше
    180 дней, delegated `openid profile email` (+ `User.Read` по умолчанию), «Assignment required».

## Последствия

- Плюс: Open WebUI, LiteLLM и Platform API доверяют одному issuer; новый IdP клиента не меняет код.
- Плюс: вход без Entra возможен (аварийный пользователь с TOTP).
- Плюс: проверено на стенде: вход с TOTP, обмен токена с `aud` = `mcp-m365`, отказ в обмене на
  чужую аудиторию, бэкап и восстановление БД `keycloak` и секретов.
- Минус: ещё один сервис для DevOps (обновления, бэкап, около 0,7-1,5 GB RAM).
- Минус: актор агента не виден в токене до перехода delegation в supported; источник истины по
  действиям агента: аудит Platform API.
- Минус: доступ к Graph зависит от PoC Identity Brokering API V2 (запасной вариант описан).
- Минус: Admin API на стенде доступен на том же адресе, что и вход пользователей.
- Сделать: проверка входа через Entra на стенде после регистрации приложения; PoC Graph на шаге
  MCP M365; TLS, отдельный адрес админ-консоли и сверка realm с дрейфом перед сервером.

## План выхода (как заменить)

Сервисы зависят только от OIDC (discovery, JWKS) и RFC 8693. Пользователи в Platform API
привязаны к `(iss, sub)` и `entra_oid`, поэтому замена Keycloak на другой OIDC-провайдер требует
сопоставить старые `sub` с новыми по `entra_oid` (или по SCIM) и перенести realm-настройки
(клиенты, scopes, брокер). Отказ от собственного IdP в пользу Entra напрямую: Platform API
принимает issuer Entra, Graph через OBO. Оценка: 2-3 недели.

## Допущения и открытые вопросы

- Допущение: в ID-токене Entra v2 есть `oid` и `tid` (scope `profile`); `email`, `given_name`,
  `family_name` настраиваются как optional claims приложения.
- Допущение: один realm на инсталляцию достаточен; организации клиентов разделены
  инсталляциями (ADR-0005), а не realm.
- [ ] Вход через Entra на стенде: после регистрации приложения (`scripts/set-entra.sh`).
- [ ] PoC Identity Brokering API V2 для Graph на шаге MCP M365.
- [ ] Сверка realm с дрейфом на сервере: keycloak-config-cli или свой apply.
- [ ] Group/app roles из Entra в роли Keycloak для Cerbos (ADR-0009).
- [ ] Сертификат вместо client secret для брокера Entra.
- [ ] HA Keycloak (2 узла) на продакшне: кэш и сессии.

## Ссылки

- https://www.keycloak.org/securing-apps/token-exchange
- https://www.keycloak.org/docs/26.8.2/release_notes/
- https://www.keycloak.org/docs/26.8.2/upgrading/
- https://www.keycloak.org/server/configuration
- https://www.keycloak.org/server/vault
- https://www.keycloak.org/server/containers
- https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow
- https://www.rfc-editor.org/rfc/rfc8693
