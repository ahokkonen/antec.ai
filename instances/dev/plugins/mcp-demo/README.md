# mcp-demo

Тестовый MCP-сервер dev-инсталляции ([ADR-0010](../../../../docs/adr/0010-agent-runtime.md)):
Streamable HTTP, ревизия 2026-07-28 без сессий, `POST /mcp`. Проверяет токен пользователя,
полученный Platform API обменом (подпись по JWKS Keycloak, `iss`, `aud` = `mcp-demo`,
`azp` = `platform-api`).

| Инструмент | Манифест | Что делает |
| --- | --- | --- |
| `get_time` | `read_only: true` | Текущее время UTC |
| `create_note` | с подтверждением | Сохраняет заметку (в памяти) от имени `sub` токена; в журнал пишется `note created owner=<sub>` |

Настройки окружения: `MCP_DEMO_ISSUER`, `MCP_DEMO_JWKS_URL`, `MCP_DEMO_AUDIENCE` (по умолчанию
`mcp-demo`), `MCP_DEMO_AUTHORIZED_PARTY` (по умолчанию `platform-api`).

```bash
uv sync --locked && uv run --locked ruff check . && uv run --locked mypy && uv run --locked pytest
```
