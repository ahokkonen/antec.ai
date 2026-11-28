# ADR-0013: LLM-observability: OTel и VictoriaMetrics, VictoriaLogs, VictoriaTraces, Grafana, без Langfuse

- Статус: предложено
- Дата: 2026-11-26
- Ответственный: CTO
- Связанные: ADR-0003, ADR-0004, ADR-0006, ADR-0010, ADR-0012, ADR-0014, ADR-0016,
  [policy/allowed-versions.yaml](../../policy/allowed-versions.yaml),
  [deploy/compose/observability](../../deploy/compose/observability)

## Контекст

Пилот (roadmap) требует LLM-observability и доступность 99,5% за месяц, MVP (недели 9-10)
требует наблюдаемость и алерты. Ограничения:

- ADR-0012, п. 6: аудит отдельно от телеметрии; в телеметрии нет содержимого, имён и email,
  сбор содержимого GenAI выключен, связь с аудитом по `request_id`. Срок хранения телеметрии на
  сервере 30 дней.
- ADR-0003: единственная СУБД PostgreSQL; исключение только для хранилищ метрик и журналов
  (VictoriaMetrics, VictoriaLogs), ClickHouse для Langfuse прямо исключён.
- В Platform API OTel не подключён; `model_calls` уже хранит токены и `cost_usd`, `agent_runs`
  статусы запусков.

Факты (проверено 2026-11-26, граница карантина 2026-11-19):

- Langfuse 4.86 (релизы несколько раз в день), MIT кроме каталогов `ee/`, принадлежит ClickHouse
  Inc. Self-hosted compose: web, worker, ai-gateway, ClickHouse, MinIO (AGPL-3.0), Redis 7
  (с 7.4 RSAL/SSPL). Opik 2.3.x (Apache-2.0): ClickHouse, MySQL, Redis, MinIO; 5 GHSA за 2026 год,
  включая обход RBAC. Arize Phoenix: Elastic License 2.0 (не OSI).
- OTel Collector contrib 0.165.0 (2026-11-07, core 1.71.0), Apache-2.0, подписан cosign keyless
  (`opentelemetry-collector-releases/.github/workflows/base-release.yaml@refs/tags/v0.165.0`).
  GHSA 2026 года закрыты в 0.165.0 или относятся к неиспользуемым компонентам.
- VictoriaMetrics 1.156.0, VictoriaLogs 1.54.0, VictoriaTraces 0.13.0, Apache-2.0. Образы без
  подписи и provenance; digest индекса совпадает на docker.io и quay.io. VictoriaMetrics: 11 GHSA
  за 2026 год (medium), закрыты к 1.154.0. Образы Logs и Traces без shell. Пути OTLP проверены
  по исходникам: `/opentelemetry/v1/metrics`, `/insert/opentelemetry/v1/logs`,
  `/insert/opentelemetry/v1/traces`; у VictoriaTraces есть Jaeger Query API (`/select/jaeger`).
- Jaeger v2.23.0 (Apache-2.0, CNCF graduated) без GHSA за 2026 год.
- Grafana 13.3.2 (2026-11-24, security-релиз: 2 CVE), AGPL-3.0, допустима отдельным
  немодифицированным сервисом. 14 записей OSV с июня. Подписи нет, только Docker Hub.
  Perses 0.55.1 (Apache-2.0): 16 GHSA за 2026 год, включая критические XSS и SSRF.
- OTel GenAI semconv вынесены в отдельный репозиторий (v0.1.0 от 2026-11-21), статус Development.
  Pydantic AI пишет OTel по GenAI semconv; `InstrumentationSettings(include_content=False,
  include_binary_content=False)` исключает промпты, ответы, аргументы и результаты инструментов,
  у исключений остаётся только тип.
- `opentelemetry-sdk` и `opentelemetry-exporter-otlp-proto-http` 1.45.2 (Apache-2.0) требуют
  `opentelemetry-semantic-conventions==0.66b2`, `opentelemetry-exporter-otlp-common==0.66b2`,
  `opentelemetry-exporter-http-transport==0.66b2`: по PEP 440 это пре-релизы. pincheck до этого ADR
  пре-релизы Python-пакетов не проверял.

## Варианты

| Вариант | Версия, лицензия | Плюсы | Минусы | Стоимость поддержки |
| --- | --- | --- | --- | --- |
| **OTel + VictoriaMetrics/Logs/Traces + Grafana** | Apache-2.0, Grafana AGPL отдельно | Стандартный OTLP, бэкенд заменяем; уже в архитектуре; без новых СУБД; метрики и трейсы без содержимого | LLM-представления строим сами (дашборды, представление расходов) | Средняя: 5 сервисов, один стиль конфигурации |
| Langfuse self-hosted | 4.86, MIT + `ee/` | Готовый UI трейсов LLM, оценки, промпты | Основная ценность (содержимое) запрещена ADR-0012; ClickHouse, Redis, S3 против ADR-0003; MinIO AGPL; релизы несколько раз в день | Высокая |
| Opik | 2.3.x, Apache-2.0 | То же, что Langfuse | Те же зависимости; GHSA с обходом RBAC | Высокая |
| Только метрики (`prometheus-client`) и журналы | Apache-2.0 | Без беточных пакетов OTel | Нет трейсов запроса через фасад, агента, MCP и Cerbos | Низкая |

Хранилище трейсов:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **VictoriaTraces 0.13** | Одно семейство с метриками и журналами; один флаг срока хранения; Jaeger API для Grafana | Версия 0.x, проект моложе |
| Jaeger v2 + Badger | Зрелый, CNCF graduated | Второй стиль конфигурации и хранилища |

Алерты:

| Вариант | Плюсы | Минусы |
| --- | --- | --- |
| **Grafana alerting, правила как код** | Без новых сервисов | Алерты зависят от Grafana |
| vmalert + Alertmanager | Независимы от UI | Ещё два сервиса |

## Решение

1. **Без Langfuse и Opik в пилоте.** Оценки качества и управление промптами решаются отдельным
   ADR, когда появится класс данных, для которого содержимое разрешено хранить.
2. **Platform API на OTel SDK 1.45.x**, трейсы, метрики и журналы по OTLP/HTTP в
   `otel-collector`. Агенты: `Agent.instrument_all(InstrumentationSettings(include_content=False,
   include_binary_content=False))`. Свои спаны: фасад, MCP-вызов, Cerbos, LiteLLM. Пакеты
   `opentelemetry-instrumentation-*` не используются. Атрибуты: `antec.request_id`, модель, агент,
   статус, токены; без id пользователя, email, имён и текстов. Без адреса Collector
   (`PLATFORM_OTEL_ENDPOINT` пуст) телеметрия выключена. Встроенная телеметрия FastAPI (0.163+:
   свои спаны, метрики и журналы с реальным путём запроса) выключена, спан запроса один, по шаблону
   маршрута; проверки здоровья без спанов. Журналы Platform API остаются JSON в stdout.
3. **Пре-релизы Python (уточнение ADR-0016).** pincheck проверяет `uv.lock`: версия a, b, rc
   или dev по PEP 440 = ошибка PIN034. Исключение только в `python_prerelease_allowed` реестра:
   пакет, шаблон версий (`versions`, на всю версию) и причина; сейчас там три пакета OTel с
   шаблоном `0\.\d+b\d+`, которые выпускаются в паре со стабильным SDK 1.x. Другой пре-релиз
   того же пакета (например, `1.0.0rc1`) запрещён.
4. **Бэкенды** в compose ядра, профиль `observability`, сети `telemetry` (Platform API ->
   Collector) и `observability` (Collector -> хранилища -> Grafana), обе internal, портов на хост
   нет (кроме Grafana на `127.0.0.1`, п. 5):
   - OTel Collector contrib: только приёмник OTLP/HTTP, `memory_limiter`, `batch` и процессоры
     приватности, которые срабатывают, даже если источник прислал содержимое:
     `attributes/privacy` удаляет тексты GenAI по semconv, атрибуты содержимого Pydantic AI и
     Logfire (`pydantic_ai.all_messages`, `final_result`, `pydantic_ai.decision.*`, `logfire.msg`),
     `enduser.*`, `user.*`, HTTP-заголовки, query URL и тексты исключений; `filter/privacy`
     оставляет у спанов только события `exception`; `transform/privacy` очищает тело
     журналов-событий GenAI. Проверено на стенде канарейкой во всех этих местах;
   - VictoriaMetrics, VictoriaLogs, VictoriaTraces со сроком `ANTEC_TELEMETRY_RETENTION`
     (по умолчанию 30 дней). Образы без пользователя запускаются от root без capabilities,
     с корнем только для чтения.
5. **Grafana 13.3.x** отдельным немодифицированным контейнером: вход только через Keycloak
   (OIDC), без анонимного доступа и локальной формы входа, порт `127.0.0.1`; источники данных,
   дашборды и правила алертов как код (provisioning). Дашборд LLM: задержка, токены и ошибки по
   модели и агенту, вызовы инструментов, отказы Cerbos; расходы из `model_calls` через роль
   PostgreSQL `platform_metrics`, которая видит только агрегирующее представление без текстов и
   идентификаторов. Патчи Grafana за 72 ч.
6. **Алерты** правилами Grafana: доступность фасада, доля ошибок модели, отставание
   `platform-worker`, место на диске. Получатель: email из манифеста, SMTP в OpenBao.
7. **Манифест v6:** `retention.telemetry_days` (по умолчанию 30, не больше 90) и
   `observability.enabled`, `observability.alert_email`. v1-v5 остаются валидными (ADR-0014).
   Рендер передаёт адрес Collector Platform API и platform-worker, копирует правила алертов ядра
   и пишет получателя в `.rendered/grafana/alerting`. platform-worker шлёт метрики проходов
   (`antec.worker.sweeps`, `antec.worker.closed`), оба сервиса `antec.service.up`. Состояние
   Grafana в SQLite своего тома (пользователи и настройки восстанавливаются provisioning'ом и
   входом через Keycloak), бэкап тома не нужен. SMTP в OpenBao (`antec/smtp`,
   `scripts/set-smtp.sh`), только STARTTLS.
8. **LiteLLM** без своей OTel-телеметрии в пилоте: каждый вызов модели и так виден в Platform API.
9. **Сэмплирование** 100% трейсов (до 500 пользователей, 30 дней).
10. **Проверка отсутствия содержимого:** smoke-проверка шлёт запрос с канарейкой и ищет её в
    VictoriaLogs и VictoriaTraces; находка = ошибка.
11. **Порядок:** PR A: этот ADR, реестр, PIN034, compose-профиль; PR B: OTel в Platform API и
    проверка канарейки; PR C: Grafana, дашборды, алерты, манифест v6.

## Последствия

- Плюс: одна телеметрия OTLP для всех сервисов, бэкенд заменяем без изменения кода.
- Плюс: содержимое не уходит в телеметрию ни у источника, ни после Collector.
- Плюс: pincheck впервые ловит пре-релизы Python-пакетов.
- Минус: LLM-дашборды и представление расходов поддерживаем сами.
- Минус: образы VictoriaMetrics и Grafana не подписаны; для Grafana digest подтверждён одним
  источником.
- Минус: VictoriaTraces 0.x; при проблемах замена на Jaeger v2 (тот же OTLP, Grafana через Jaeger
  API).
- Минус: Victoria-образы работают от root (без capabilities, корень только для чтения).
- Сделать: на сервере журналы Docker (`local`) остаются на хосте; перевод журналов контейнеров в
  VictoriaLogs отдельной задачей.

## План выхода (как заменить)

Граница: OTLP/HTTP от сервисов и Jaeger/Prometheus/LogsQL API для Grafana. Замена хранилищ
меняет только экспортёры Collector и источники данных Grafana. Langfuse или другой
LLM-специфичный бэкенд подключается ещё одним экспортёром Collector без изменения Platform API.
Оценка: 1-2 дня на компонент.

## Допущения и открытые вопросы

- Допущение: до 500 пользователей объём трейсов при 100% сэмплировании помещается в 30 дней на
  одном хосте (проверить в пилоте по размеру томов).
- [ ] Метрики Prometheus у LiteLLM OSS и отсутствие в них промптов: проверить при подключении.
- [ ] Журналы контейнеров в VictoriaLogs на сервере.
- [ ] Оценки качества ответов (evals) и управление промптами: отдельный ADR.

## Ссылки

- https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.165.0
- https://github.com/open-telemetry/semantic-conventions-genai
- https://github.com/pydantic/pydantic-ai/blob/v2.75.0/docs/logfire.md
- https://github.com/VictoriaMetrics/VictoriaTraces/blob/v0.13.0/docs/victoriatraces/querying/README.md
- https://github.com/langfuse/langfuse/blob/main/docker-compose.yml
- https://github.com/grafana/grafana/releases/tag/v13.3.2
- https://pypi.org/project/opentelemetry-sdk/1.45.2/
