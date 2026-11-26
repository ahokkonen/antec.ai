# tools/instance

Манифест инсталляции antec.ai (ADR-0008): схема v1-v5 (v1-v4 устарели, удаление не раньше 0.3.0, ADR-0014), проверка политик инсталляции, рендер
производных файлов и создание нового репозитория инсталляции из `templates/instance`.

```bash
uv run --project tools/instance --locked antec-instance validate instances/dev
```

```bash
uv run --project tools/instance --locked antec-instance render instances/dev --out instances/dev/.rendered
```

```bash
uv run --project tools/instance --locked antec-instance init ../instance-acme --id acme --name "Acme Oy"
```

| Команда | Что делает |
| --- | --- |
| `validate` | Схема `instance.yaml`, `core.version` = `VERSION` ядра, scope политик в `policies/` = id инсталляции, файлы инструкций агентов; MCP-серверы только ревизии 2026-07-28 (MCP-клиент ядра); предупреждения о моделях и инструментах, недоступных по классу данных, и о незаполненном комплаенсе (контролёр, уведомление, ZDR) |
| `render` | То же и запись `litellm/models.yaml` (`model_list` для `include` LiteLLM), `platform-api/agents.json` (агенты, ADR-0010), `platform-api/mcp_servers.json` (MCP-серверы, белые списки и классы инструментов), `platform-api/models.json` (допуски моделей, ADR-0011), `platform-api.env` (`PLATFORM_*`), `platform-worker.env`, `litellm.env`, `open-webui.env` (уведомление об ИИ, ADR-0015), `compliance/record.md` и `compliance/dpia-facts.md` (черновики реестра обработки и фактов DPIA по `policy/llm-providers.yaml`, ADR-0015) (сроки хранения, ADR-0012), `instance.json` (сводка для скриптов стенда: модели, агенты, аудитории MCP); каждый сервис монтирует только свой подкаталог |
| `init` | Копия шаблона с подстановкой id, названия и версии ядра; каталог должен быть пустым |
| `core-pin` | Скачивает `release.json` и bundle релиза `core.version` (или берёт из `--release-dir`), проверяет keyless-подпись `cosign verify-blob` (identity `release.yml@refs/tags/vX.Y.Z` ядра), совместимость манифеста (`schema_version`, ревизии MCP) и пишет `core-images.yaml`. `validate` сверяет его версию с `core.version`, `render` пишет `compose.core-images.yaml` (образы ядра по digest, `build: !reset null`) |
| `core-info` | JSON для `release.json` (ADR-0014): версия ядра, поддерживаемые и устаревшие `schema_version`, ревизии MCP-клиента ядра |

Код выхода: 0 успех, 1 манифест или политики не прошли проверку, 2 ошибка использования.
Компиляцию и тесты политик делает Cerbos: `deploy/compose/scripts/policies-check.sh`.
