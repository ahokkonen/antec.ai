# antec.ai

Self-hosted AI-платформа на open source для организаций: веб-чат, агенты, MCP-инструменты,
LLM-шлюз, аутентификация, авторизация, гардрейлы и аудит. Docker-first, без привязки к вендорам.

Статус: проектирование, v1 (MVP).

Это репозиторий ядра. Каждая инсталляция (Mtech, клиенты) живёт в своём репозитории
`instance-<org>` с закреплённой версией ядра ([ADR-0005](docs/adr/0005-core-and-instance-repositories.md)).

- Документация: [docs/README.md](docs/README.md)
- Правила для AI-ассистентов и людей: [AGENTS.md](AGENTS.md)
- Разрешённые версии компонентов: [policy/allowed-versions.yaml](policy/allowed-versions.yaml)
