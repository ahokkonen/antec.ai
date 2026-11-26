# Инсталляция __INSTANCE_NAME__ (`__INSTANCE_ID__`)

Репозиторий инсталляции antec.ai (ADR-0005, ADR-0008). Создан `antec-instance init` из
шаблона ядра __CORE_VERSION__.

| Путь | Что там |
| --- | --- |
| `core/` | Ядро antec.ai, git submodule на тег релиза |
| `instance.yaml` | Манифест: модели, MCP-серверы, настройки Platform API |
| `core-images.yaml` | Образы ядра по digest из подписанного `release.json` (`antec-instance core-pin`, ADR-0014) |
| `policies/` | Политики Cerbos в scope `__INSTANCE_ID__` и их тесты |
| `plugins/` | Код плагинов только этой инсталляции |
| `compose.override.yaml` | Сервисы плагинов поверх compose ядра |
| `docs/adr/` | Отклонения от стандарта ядра |
| `docs/compliance/` | DPIA, цель контроля, уведомление о конфиденциальности, правила работы с ИИ (ADR-0015) |

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

## Закрепить образы ядра

Образы ядра берутся из GHCR по digest из подписанного `release.json` релиза
`core.version` (ADR-0014). Нужен cosign версии из реестра ядра (`policy/allowed-versions.yaml`,
`components.cosign`). Без `core-images.yaml` образы ядра собираются локально из `core/`.
Пакеты `ghcr.io/ahokkonen/antec-*` публичные; если пакет закрыт, перед `up.sh` нужен
`docker login ghcr.io` с токеном `read:packages`, иначе `up.sh` остановится на загрузке образа.

```bash
uv run --project core/tools/instance --locked antec-instance core-pin .
```

```bash
uv run --project core/tools/pincheck --locked pincheck --policy core/policy/allowed-versions.yaml --require-digest --core-images core-images.yaml .
```

## Обновить ядро

1. Прочитать разделы «Несовместимые изменения» и «Обновление» в `core/CHANGELOG.md` нового
   релиза. Поддерживаются только две последние minor-версии ядра (ADR-0005, п. 4).
2. Перевести `core/` на новый тег, поправить `core.version` и манифест:

   ```bash
   git -C core fetch --tags && git -C core checkout vX.Y.Z
   ```

3. `core-pin`, `validate` и pincheck (команды выше), коммит.
4. `up.sh`: перед миграциями БД он сам делает бэкап и останавливается, если БД новее ядра
   (понижение версии ядра не поддерживается, откат только восстановлением из бэкапа).
5. После запуска: `core/deploy/compose/scripts/audit-verify.sh` и smoke-проверки.

Повторной генерации из шаблона нет.
