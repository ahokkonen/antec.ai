# PostgreSQL 18.7 + pgvector 0.8.7 (ADR-0003, ADR-0017).
# База закреплена по digest из policy/allowed-versions.yaml (components.postgresql).
# Директива syntax= не используется: она тянет образ фронтенда BuildKit вне реестра.
FROM docker.io/library/postgres:18.7@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a

# Пакет PGDG с точной версией (components.pgvector). Репозиторий apt.postgresql.org
# и его ключ уже настроены в базовом образе; apt проверяет подпись.
ARG PGVECTOR_PKG_VERSION=0.8.7-1.pgdg13+2

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector=${PGVECTOR_PKG_VERSION}"; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    test -f "/usr/share/postgresql/${PG_MAJOR}/extension/vector.control"

LABEL org.opencontainers.image.title="antec-postgres" \
      org.opencontainers.image.description="PostgreSQL 18.7 + pgvector 0.8.7 для antec.ai" \
      org.opencontainers.image.version="18.7-pgvector0.8.7"
