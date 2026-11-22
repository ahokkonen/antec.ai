# Platform API (ADR-0006). База и uv закреплены по digest из policy/allowed-versions.yaml
# (components.python, components.uv). Директива syntax= не используется: она тянет образ
# фронтенда BuildKit вне реестра.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1
WORKDIR /app
# Сначала только зависимости: слой не пересобирается при изменении кода.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
# Без uv, исходников и инструментов сборки: только окружение с пакетом.
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Пользователь nobody (65534): владелец секретов в томе, как у LiteLLM и Open WebUI.
USER 65534:65534
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
ENTRYPOINT ["antec-platform"]
CMD ["serve"]

LABEL org.opencontainers.image.title="antec-platform-api" \
      org.opencontainers.image.description="Platform API antec.ai: OpenAI-совместимый фасад, пользователи, история, аудит" \
      org.opencontainers.image.version="0.1.0"
