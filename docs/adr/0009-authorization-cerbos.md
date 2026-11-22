# ADR-0009: Авторизация в Platform API: Cerbos по AuthZEN, роли из Keycloak, политики как код

- Статус: предложено
- Дата: 2026-11-22
- Ответственный: CTO
- Связанные: ADR-0004, ADR-0005, ADR-0006, ADR-0007, ADR-0008 (в очереди), ADR-0012 (в очереди),
  [architecture/overview.md](../architecture/overview.md), [policy/allowed-versions.yaml](../../policy/allowed-versions.yaml)

## Контекст

Platform API принимает токен пользователя и пускает к любой логической модели из конфигурации
(ADR-0006). Нужна проверка прав: кто может вызывать какую модель, позже агента и инструмент
(overview.md, `PolicyClient.check(subject, action, resource)`). Агент не получает больше прав,
чем пользователь, и каждое решение попадает в аудит (AGENTS.md, правило 5).

Факты (проверено 2026-11-22):

- Cerbos 0.57.0 от 2026-10-23 (Apache-2.0), вне карантина; патч-релизов линии нет. OSV по
  `github.com/cerbos/cerbos` пусто, опубликованных GHSA нет. В 0.57.0 исправлены излишне
  разрешающие query plans при политиках только с DENY и повторное использование результата
  условия чужого правила в политиках ролей и принципалов.
- Образ `ghcr.io/cerbos/cerbos:0.57.0` подписан cosign keyless
  (`release.yaml@refs/tags/v0.57.0`), статический бинарник, пользователь в образе не задан (root),
  встроенный `cerbos healthcheck`.
- С 0.48 Cerbos реализует AuthZEN Authorization API 1.0 (OpenID): `/access/v1/evaluation`,
  `/access/v1/evaluations` (пакет), `/.well-known/authzen-configuration`. Роли передаются плоским
  свойством `subject.properties["cerbos.roles"]` (так читает `internal/svc/authzen_svc.go` 0.57.0;
  вложенный объект `cerbos.roles` из документации не работает, проверено на стенде), остальные
  свойства становятся атрибутами. Без ролей Cerbos подставляет роль = `subject.type`.
- По умолчанию включены телеметрия (`telemetry.disabled: false`) и API Explorer; Admin API
  выключен. Политики читаются с диска (`storage.driver: disk`), Cerbos Hub платный и не нужен.
  Встроенный `cerbos compile` компилирует политики и прогоняет их тесты.
- Ролей в токене Platform API сейчас нет: у клиента `open-webui` есть только роли самого
  Open WebUI (`roles`: user, admin).

## Варианты

PDP:

| Вариант | Версия, лицензия | Плюсы | Минусы | Стоимость поддержки |
| --- | --- | --- | --- | --- |
| **Cerbos** | 0.57.0, Apache-2.0 | Политики YAML, понятные без программирования; встроенные тесты политик; AuthZEN; без БД | Ещё один сервис; язык условий CEL | Низкая |
| OPA | Apache-2.0 | Стандарт де-факто, гибкий | Rego новый для команды; тесты и модель ролей писать самим; AuthZEN только через шлюз | Средняя |
| Проверки в коде Platform API | - | Ноль компонентов | Политики в коде, меняются релизом; нет единой точки для агентов и MCP | Растёт с числом правил |

Протокол Platform API -> PDP:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **AuthZEN (HTTP, httpx)** | Стандарт OpenID; PDP заменяем (OPA, OpenFGA и др. через шлюзы AuthZEN); без SDK | Реализован в Cerbos частично; роли в вендорском поле `properties.cerbos.roles` |
| Собственный API Cerbos (CheckResources) через адаптер | Полный набор возможностей | Привязка к Cerbos в контракте |

Источник ролей:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Роли клиента `platform-api` в Keycloak, claim `platform_roles`** | Один источник для всех IdP; роль по умолчанию для всех пользователей realm | Роли назначаются в Keycloak |
| App roles или группы Entra через мапперы брокера | Управление в Entra | Нужны правки регистрации приложения; у других IdP свои механизмы |

## Решение

1. **Cerbos 0.57.0** как PDP Platform API. Политики только с диска, Cerbos Hub не используется.
2. **Контракт: `PolicyClient` в Platform API по AuthZEN** (HTTP, httpx, без SDK). Адаптер
   `antec_platform/adapters/authzen.py` знает о полях Cerbos (`properties.cerbos.roles`); остальной
   код видит только `check(principal, action, resource)` и пакетную проверку. Субъект без ролей
   до PDP не доходит (отказ), а роль `user` (значение `subject.type`) не используется в политиках.
3. **Роли:** роли клиента `platform-api` в Keycloak. `platform-user` входит в роли realm по
   умолчанию (доступ к realm ограничивает IdP, для Entra «Assignment required»), `platform-admin`
   назначается явно. Client scope `platform-api` (аудитория Platform API) дополнительно кладёт в
   access token claim `platform_roles` с ролями этого клиента. Соответствие групп и app roles
   Entra ролям Keycloak: открытый вопрос ADR-0007, решается, когда понадобятся разные права.
4. **Что охраняется в этом шаге:** вид ресурса `model` (id = логическое имя), действие `invoke`.
   `POST /v1/chat/completions` проверяет `invoke` до вызова шлюза; `GET /v1/models` отдаёт только
   модели с разрешением (одна пакетная проверка). Виды `agent` и `tool` добавляются вместе с
   агентом и MCP. Политика по умолчанию: `platform-user` может вызывать `chat-default` и
   `chat-fast`; всё, что не разрешено явно, запрещено.
5. **Отказ PDP = запрет:** Cerbos недоступен или ответ не разобран, значит 503 `policy_unavailable`
   (а не разрешение); `/readyz` проверяет Cerbos.
6. **Аудит:** источник истины `audit_events` Platform API (ADR-0006, п. 8). Отказ политики пишется
   событием `policy.deny` (действие, ресурс, роли); разрешённый вызов записывается в `model.call`
   как и раньше. Аудит Cerbos выключен, тела запросов Cerbos не логирует.
7. **Политики как код:** `policies/cerbos/` в ядре с тестами политик (`cerbos compile` в CI тем же
   образом по digest). На стенде каталог монтируется только для чтения, изменения применяются
   перезапуском (`watchForChanges` выключен). Как инсталляция дополняет или переопределяет
   политики ядра, решает ADR-0008.
8. **Стенд:** сервис `cerbos` в новой internal-сети `policy` (видит только `platform-api`), порт
   на хост не публикуется, HTTP без TLS только на стенде; пользователь 65534, ФС только для
   чтения, `cap_drop: ALL`, 256 MB. Выключены телеметрия, API Explorer, Admin API, аудит.

## Последствия

- Плюс: права на модели меняются политикой и ролью, без релиза Platform API; тот же механизм
  для агентов и инструментов.
- Плюс: PDP заменяем на любой с AuthZEN; политики проверяются тестами в CI.
- Минус: ещё один сервис и точка отказа на пути каждого запроса (отказ = 503).
- Минус: роли назначаются в Keycloak, а не в Entra, до решения о сопоставлении групп.
- Минус: смена роли действует только с новым access token (до 15 минут у Open WebUI).

## План выхода (как заменить)

Контракт AuthZEN: другой PDP (OPA через шлюз AuthZEN, OpenFGA, Topaz) подключается сменой
адреса и адаптера `authzen.py`, политики переписываются на его язык по тестам из
`policies/cerbos/`. Оценка: 1-2 недели.

## Допущения и открытые вопросы

- Допущение: решения Cerbos без кэша в Platform API укладываются в бюджет задержки (локальная
  сеть, единицы миллисекунд); кэш при необходимости после измерений.
- [ ] Сопоставление групп и app roles Entra ролям Keycloak (ADR-0007).
- [ ] Политики инсталляций поверх политик ядра (ADR-0008).
- [ ] TLS между Platform API и Cerbos на сервере.
- [ ] OpenFGA для прав на документы (пилот, индексация OneDrive): отдельный ADR.

## Ссылки

- https://docs.cerbos.dev/cerbos/latest/api/index.html
- https://docs.cerbos.dev/cerbos/latest/configuration/index.html
- https://docs.cerbos.dev/cerbos/latest/releases/v0.57.0.html
- https://openid.github.io/authzen/
