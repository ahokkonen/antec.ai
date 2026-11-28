# Изменения ядра antec.ai

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии по
[semver](https://semver.org/lang/ru/) с правилами ADR-0014: до 1.0.0 несовместимые изменения
только в minor. Разделы версии: Добавлено, Изменено, Несовместимые изменения, Исправлено,
Безопасность, Обновление. «Обновление» обязательно, если есть миграции БД, новая
`schema_version` манифеста или несовместимые изменения.

## [Не выпущено]

### Добавлено

- Наблюдаемость (ADR-0013): профиль compose `observability` с OTel Collector, VictoriaMetrics,
  VictoriaLogs и VictoriaTraces; фильтр приватности в Collector удаляет тексты GenAI,
  пользователя и HTTP-заголовки.
- Platform API: трейсы и метрики OTel без содержимого и id пользователя (`PLATFORM_OTEL_ENDPOINT`,
  ADR-0013); встроенная телеметрия FastAPI выключена.
- Grafana 13.3 (профиль `observability`): вход через Keycloak (клиент `grafana`, роли `viewer` и
  `admin`), дашборд LLM и Platform API, алерты на email (`scripts/set-smtp.sh`), роль PostgreSQL
  `platform_metrics` только с агрегатами (ADR-0013).
- Манифест `schema_version: 6`: `observability.enabled`, `observability.alert_email`,
  `retention.telemetry_days`; метрики platform-worker.
- pincheck PIN034: пре-релизы Python-пакетов в `uv.lock` запрещены, исключения в
  `python_prerelease_allowed` реестра.
### Обновление

- Манифест можно перевести на `schema_version: 6` и включить `observability.enabled`; v5
  остаётся валидным. `up.sh` сам создаст роль `platform_metrics`, клиента Keycloak `grafana` и
  секреты в OpenBao.

## [0.1.0] - 2026-11-26

### Добавлено

- Первый релиз ядра: Platform API (OpenAI-совместимый фасад, пользователи, история, аудит,
  ADR-0006), авторизация Cerbos (ADR-0009), манифест инсталляции v5 и шаблон
  `instance-<org>` (ADR-0008, 0011, 0012, 0015), агенты на Pydantic AI и MCP-инструменты с
  подтверждением (ADR-0010), классы данных (ADR-0011), аудит до действия с цепочкой хэшей и
  подписанными контрольными точками, сроки хранения и права субъекта (ADR-0012), уведомление и
  метка ИИ (ADR-0015), стенд docker compose (ADR-0017, 0018), pincheck (ADR-0016).
- Релизный процесс ядра (ADR-0014): единая версия в `VERSION`, `tools/release`, CHANGELOG,
  публикация подписанных образов в GHCR с SBOM и provenance, `release.json`.
- Обновление инсталляции (ADR-0014): `antec-instance core-pin` проверяет подпись
  `release.json` и пишет `core-images.yaml`; `render` подменяет локальную сборку образов ядра
  образами по digest; `pincheck --core-images`, поддержка `!reset` и `!override` Compose.
- `antec-platform migrate --plan` и флаг `IRREVERSIBLE` у миграций; `up.sh` делает бэкап перед
  миграциями существующей БД (`--no-backup` для стенда разработки).
- CI: проверка обновления БД с предыдущего релиза (job `upgrade`).

### Изменено

- `up.sh` и `antec-platform migrate` отказываются работать с БД на ревизии, неизвестной ядру
  (понижение версии ядра); откат только восстановлением из бэкапа.
- `schema_version` 1-4 манифеста инсталляции устарели: `validate` предупреждает, удаление не
  раньше 0.3.0 (ADR-0014, п. 3).

### Обновление

- Первый релиз: обновлять нечего. Новая инсталляция создаётся из шаблона
  (`antec-instance init`), образы ядра закрепляются `antec-instance core-pin`
  (`templates/instance/README.md`).
