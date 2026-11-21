# Обзор кандидатов, 21.11.2026

Снимок фактов на дату. Не обновляется; для перепроверки создаётся новый файл.
Источник истины по версиям: `policy/allowed-versions.yaml`.

## Центральное управление

| Компонент | Кандидат | Версия | Лицензия | Замечания |
| --- | --- | --- | --- | --- |
| OIDC | Keycloak | 26.8.2 (05.11.2026) | Apache-2.0 | CNCF Incubating; в 26.8.x закрыто более 10 CVE |
| OIDC | Authentik | 2026.8 | MIT + Enterprise | Agent accounts, DCR, PAM платные |
| OIDC | Zitadel | v3+ | AGPL-3.0 | Сменил лицензию с Apache в 2025 |
| LLM-шлюз | LiteLLM | 1.108.x LTS, 1.110.0 (18.11.2026) | MIT + enterprise/ | Компрометация 1.82.7, 1.82.8 (март) и 1.107.0 (21.10.2026); SSO >5, JWT, аудит платные |
| LLM-шлюз | Bifrost | HTTP 2.3.2 (20.10.2026) | Apache-2.0 | Go; ключи и бюджеты в OSS |
| LLM/MCP-шлюз | agentgateway | 1.6.0 (21.10.2026) | Apache-2.0 | Linux Foundation / AAIF; MCP 2026-07-28 с v1.4 |
| Секреты | OpenBao | 2.7.2 (11.2026) | MPL-2.0 | Linux Foundation / OpenSSF |
| Секреты | Infisical | 2026 | MIT + проприетарный ee | SCIM, LDAP, SIEM-стриминг платные |
| Политики | Cerbos | 0.56 (10.2026) | Apache-2.0 | AuthZEN |
| Политики | OpenFGA | 1.11+ | Apache-2.0 | CNCF Incubating с 10.2025, ReBAC |
| LLM-observability | Langfuse | v4 (08.2026) | MIT + Enterprise | Куплен ClickHouse (01.2026); требует ClickHouse, Redis, S3 |
| LLM-observability | Arize Phoenix | 2026 | Elastic License 2.0 | Не OSI |
| Метрики и логи | VictoriaMetrics/Logs/Traces | 11.2026 | Apache-2.0 | |
| Дашборды | Grafana | 2026 | AGPL-3.0 | |

## Агентная платформа

| Компонент | Кандидат | Версия | Лицензия | Замечания |
| --- | --- | --- | --- | --- |
| Фреймворк | Pydantic AI | 2.x (v2.0 от 23.06.2026) | MIT | Durable через Temporal, DBOS, Prefect, Restate |
| Фреймворк | LangGraph + deepagents | 1.2 / 0.6 (05.2026) | MIT | |
| Durable | DBOS Transact | 3.1.0 (09.2026) | MIT | Только PostgreSQL; Conductor проприетарный |
| Durable | Temporal | 2026 | MIT | Прод через Kubernetes |
| Процессы | n8n | | Sustainable Use License | Не open source (не перепроверялось) |
| Парсинг | Docling | 2.9x | MIT | LF AI & Data |
| Векторы | pgvector | 0.8.7 (05.10.2026) | PostgreSQL | CVE-2026-103484 исправлена в 0.8.7 |
| Гардрейлы | LLM Guard | 0.3.16 (05.2025) | MIT | Архивирован 09.07.2026, не использовать |
| Гардрейлы | NeMo Guardrails | 0.24.1 (11.2026) | Apache-2.0 | |
| MCP-шлюз | IBM ContextForge | 1.1.x | Apache-2.0 | Серия CVE в 2026, исправлены до 1.1.0 |

## Каналы

| Компонент | Кандидат | Версия | Лицензия | Замечания |
| --- | --- | --- | --- | --- |
| Веб-чат | Open WebUI | v0.13.3 (08.11.2026), v0.13.4 (19.11.2026) | Open WebUI License | Брендинг при >50 пользователях; серия CVE доступа в 2026 |
| Веб-чат | LibreChat | 0.8.9 | MIT | Куплен ClickHouse; требует MongoDB (SSPL) |
| Веб-чат | LobeHub | 2026 | LobeHub Community License | Деривативы требуют коммерческой лицензии |

## Разработка и инфраструктура

| Компонент | Кандидат | Версия | Лицензия | Замечания |
| --- | --- | --- | --- | --- |
| Coding-агент | OpenCode | 2026 | MIT | Без встроенной песочницы |
| Coding-агент | OpenHands | Agent Canvas 1.27.1 (12.10.2026) | MIT + source-available Enterprise | Docker-песочница |
| Coding-агент | Cline | 3.85 (05.2026) | Apache-2.0 | |
| Локальные модели | Ollama | 0.35.x (10.2026) | MIT | v2 |
| Локальные модели | vLLM | 0.30.x (10.2026) | Apache-2.0 | Только GPU, v2 |
| S3 | SeaweedFS | 2026 | Apache-2.0 | MinIO CE архивирован |
| S3 | Garage | 2026 | AGPL-3.0 | |
| СУБД | PostgreSQL | 18.7 (12.11.2026) | PostgreSQL | |

## Источники

- LiteLLM: https://www.konfirmity.com/advisories/litellm-1-107-0-supply-chain-compromise ,
  https://www.mend.io/blog/litellm-1-107-0-compromised/ , https://docs.litellm.ai/docs/enterprise
- Open WebUI: https://docs.openwebui.com/license/ , https://github.com/open-webui/open-webui/releases
- Keycloak: https://www.keycloak.org/2026/11/keycloak-2682-released
- agentgateway: https://geekflare.com/open-source/agentgateway/
- Bifrost: https://github.com/maximhq/bifrost/releases
- OpenBao: https://openbao.org/community/release-notes/2-6-0/
- Cerbos: https://www.cerbos.dev/blog ; OpenFGA: https://openfga.dev/project
- Langfuse: https://langfuse.com/blog/joining-clickhouse
- Pydantic AI: https://pydantic.dev/docs/ai/project/version-policy/
- DBOS: https://simple-repository.app.cern.ch/project/dbos
- pgvector: https://www.postgresql.org/message-id/179116294741.1252.17308312335154774817%40wrigleys.postgresql.org
- PostgreSQL 18.7: https://www.postgresql.org/docs/18/release-18-7.html
- LLM Guard: https://grepture.com/blog/llm-guard-archived-migration-guide
- NeMo Guardrails: https://tech-insider.org/?p=53522
- LibreChat: https://www.librechat.ai/changelog/v0.8.9
- OpenHands, OpenCode: https://www.testmuai.com/blog/openhands-alternatives/
- Ollama, vLLM: https://www.morphllm.com/comparisons/vllm-vs-ollama
- MinIO: https://www.infoq.com/news/2025/12/minio-s3-api-alternatives
- NIS2 Финляндия: https://kyberturvallisuuskeskus.fi/en/our-activities/regulation-and-supervision/important-information-european-union-cybersecurity
