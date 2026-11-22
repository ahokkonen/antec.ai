# tools/instance

Манифест инсталляции antec.ai (ADR-0008): схема v1, проверка политик инсталляции, рендер
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
| `validate` | Схема `instance.yaml`, `core.version` = `VERSION` ядра, scope политик в `policies/` = id инсталляции |
| `render` | То же и запись `litellm-models.yaml` (`model_list` для `include` LiteLLM), `platform-api.env` (`PLATFORM_*`), `instance.json` (сводка для скриптов стенда) |
| `init` | Копия шаблона с подстановкой id, названия и версии ядра; каталог должен быть пустым |

Код выхода: 0 успех, 1 манифест или политики не прошли проверку, 2 ошибка использования.
Компиляцию и тесты политик делает Cerbos: `deploy/compose/scripts/policies-check.sh`.
