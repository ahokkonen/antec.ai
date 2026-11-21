# Документация

## Порядок чтения

1. [context/brief.md](context/brief.md): цель, части платформы, принципы, требования безопасности
2. [context/requirements.md](context/requirements.md): вводные для v1, допущения, открытые вопросы
3. [architecture/overview.md](architecture/overview.md): архитектура v1, схема, границы замены
4. [architecture/roadmap.md](architecture/roadmap.md): MVP, пилот, продакшн, v2
5. [architecture/risks.md](architecture/risks.md): риски и меры
6. [adr/README.md](adr/README.md): принятые и планируемые решения
7. [security/licensing.md](security/licensing.md): лицензии компонентов и сценарии распространения

## Соглашения

- **ADR** (`adr/NNNN-*.md`): одно решение на файл, сквозная нумерация, статус в шапке.
  Принятый ADR не редактируется по существу; изменение решения = новый ADR со ссылкой «заменяет NNNN».
- **Исследования** (`research/YYYY-MM-DD-*.md`): снимок фактов на дату. Не обновляются,
  при перепроверке создаётся новый файл.
- **Архитектура** (`architecture/`): живые документы, отражают текущее состояние и ссылаются на ADR.
- **Версии**: только в [../policy/allowed-versions.yaml](../policy/allowed-versions.yaml);
  в документах версии упоминаются для справки, источник истины реестр.
- Формат: обычный Markdown и Mermaid, без расширений конкретных инструментов.
