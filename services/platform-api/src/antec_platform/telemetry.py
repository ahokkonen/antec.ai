"""Телеметрия Platform API через API OpenTelemetry (ADR-0013).

Здесь только API OTel (стандарт, как OIDC или SQL): без настроенного SDK все вызовы ничего не
делают. SDK и экспорт по OTLP настраивает `adapters/otel.py`.

Правило ADR-0012, п. 6: в атрибутах нет содержимого (сообщений, аргументов, результатов, текстов
ошибок), имён, email и id пользователя. Связь с аудитом только по `antec.request_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from antec_platform.store import CallResult, CallStart

SCOPE = "antec_platform"
REQUEST_ID = "antec.request_id"

_tracer = trace.get_tracer(SCOPE)
_meter = metrics.get_meter(SCOPE)

# Границы гистограмм по рекомендации semconv HTTP и GenAI.
_DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300]
_TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]

http_duration = _meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Длительность HTTP-запросов к Platform API",
    explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
)
token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Токены вызовов моделей через фасад (без агентов: их считает Pydantic AI)",
    explicit_bucket_boundaries_advisory=_TOKEN_BUCKETS,
)
model_calls = _meter.create_counter(
    "antec.model.calls",
    unit="{call}",
    description="Вызовы моделей через фасад по модели и итогу",
)
policy_decisions = _meter.create_counter(
    "antec.policy.decisions",
    unit="{decision}",
    description="Решения PDP по действию и итогу",
)
mcp_requests = _meter.create_counter(
    "antec.mcp.requests",
    unit="{request}",
    description="Запросы к MCP-серверам по серверу, методу и итогу",
)


@contextmanager
def span(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, str | int | bool] | None = None,
    *,
    current: bool = True,
) -> Iterator[Span]:
    """Спан без текста исключения: тип в `error.type`, статус ERROR без описания.

    current=False: спан не становится текущим (поток ответа, который закрывается в другой
    задаче и не должен менять контекст вызывающего кода).
    """
    if not current:
        detached = _tracer.start_span(name, kind=kind, attributes=attributes)
        try:
            yield detached
        except BaseException as exc:
            _mark_error(detached, exc)
            raise
        finally:
            detached.end()
        return
    with _tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as active:
        try:
            yield active
        except BaseException as exc:
            _mark_error(active, exc)
            raise


def _mark_error(target: Span, exc: BaseException) -> None:
    target.set_attribute("error.type", type(exc).__name__)
    target.set_status(Status(StatusCode.ERROR))


def request_attributes(request_id: uuid.UUID) -> dict[str, str | int | bool]:
    return {REQUEST_ID: str(request_id)}


def record_call(call: CallStart, result: CallResult) -> None:
    """Метрики вызова модели через фасад: модель, итог, токены (ADR-0013, п. 2)."""
    base = {"gen_ai.operation.name": "chat", "gen_ai.request.model": call.model}
    model_calls.add(1, {**base, "antec.status": result.status})
    for kind, tokens in (("input", result.prompt_tokens), ("output", result.completion_tokens)):
        if tokens is not None:
            token_usage.record(tokens, {**base, "gen_ai.token.type": kind})
