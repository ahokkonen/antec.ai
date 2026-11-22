# Инсталляция __INSTANCE_NAME__ (`__INSTANCE_ID__`)

Репозиторий инсталляции antec.ai (ADR-0005, ADR-0008). Создан `antec-instance init` из
шаблона ядра __CORE_VERSION__.

| Путь | Что там |
| --- | --- |
| `core/` | Ядро antec.ai, git submodule на тег релиза |
| `instance.yaml` | Манифест: модели, MCP-серверы, настройки Platform API |
| `policies/` | Политики Cerbos в scope `__INSTANCE_ID__` и их тесты |
| `plugins/` | Код плагинов только этой инсталляции |
| `compose.override.yaml` | Сервисы плагинов поверх compose ядра |
| `docs/adr/` | Отклонения от стандарта ядра |

## Подключить ядро

```bash
git submodule add <адрес репозитория antec.ai> core
```

```bash
git -C core checkout v__CORE_VERSION__
```

## Проверить и запустить стенд

```bash
uv run --project core/tools/instance --locked antec-instance validate .
```

```bash
core/deploy/compose/scripts/policies-check.sh .
```

```bash
ANTEC_INSTANCE_DIR="$PWD" core/deploy/compose/scripts/up.sh
```

## Обновить ядро

Перевести `core/` на новый тег, прочитать changelog ядра, поправить `core.version` и манифест,
прогнать проверки. Повторной генерации из шаблона нет.
