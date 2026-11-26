# Документация

## Порядок чтения

1. [context/brief.md](context/brief.md): цель, части платформы, принципы, требования безопасности
2. [context/requirements.md](context/requirements.md): вводные для v1, допущения, открытые вопросы
3. [architecture/overview.md](architecture/overview.md): архитектура v1, схема, границы замены
4. [architecture/roadmap.md](architecture/roadmap.md): MVP, пилот, продакшн, v2
5. [architecture/risks.md](architecture/risks.md): риски и меры
6. [adr/README.md](adr/README.md): принятые и планируемые решения
7. [security/licensing.md](security/licensing.md): лицензии компонентов и сценарии распространения
8. [../deploy/compose/README.md](../deploy/compose/README.md): локальный стенд, запуск, бэкап и проверка восстановления

## Соглашения

- **ADR** (`adr/NNNN-*.md`): одно решение на файл, сквозная нумерация, статус в шапке.
  Принятый ADR не редактируется по существу; изменение решения = новый ADR со ссылкой «заменяет NNNN».
- **Исследования** (`research/YYYY-MM-DD-*.md`): снимок фактов на дату. Не обновляются,
  при перепроверке создаётся новый файл.
- **Архитектура** (`architecture/`): живые документы, отражают текущее состояние и ссылаются на ADR.
- **Провайдеры моделей**: условия (хранение, передача, DPA) в
  [../policy/llm-providers.yaml](../policy/llm-providers.yaml) с датой проверки (ADR-0015).
- **Версии**: только в [../policy/allowed-versions.yaml](../policy/allowed-versions.yaml);
  в документах версии упоминаются для справки, источник истины реестр.
  Соответствие реестру проверяет [pincheck](../tools/pincheck/README.md) в CI на каждый PR
  (ADR-0016). Локально из корня репозитория:
  `uv run --project tools/pincheck --locked pincheck --policy policy/allowed-versions.yaml --exclude tools/pincheck/tests/fixtures .`
- **Релизы**: версия ядра только в `VERSION`, изменения и заметки по обновлению в
  [../CHANGELOG.md](../CHANGELOG.md), процесс и чек-лист в [../tools/release](../tools/release/README.md)
  (ADR-0014).
- Формат: обычный Markdown и Mermaid, без расширений конкретных инструментов.
