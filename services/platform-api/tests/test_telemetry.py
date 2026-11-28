"""Телеметрия без содержимого и персональных данных (ADR-0013, п. 2; ADR-0012, п. 6).

Глобальные провайдеры OTel ставятся один раз на процесс: SDK с экспортом в память.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from conftest import Harness, TokenFactory, ok_meta
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_pydantic_ai_adapter import AGENT, Gateway, _chunk, _sse

from antec_platform import telemetry
from antec_platform.adapters.gateway import GatewayResponse
from antec_platform.adapters.pydantic_ai import PydanticAIRuntime, instrument_agents
from antec_platform.agents import Turn

pytestmark = pytest.mark.anyio

CANARY = "canary-7f3a9c"
USER = "user-canary-42"

_EXPORTER = InMemorySpanExporter()
# Дельта: каждое чтение возвращает только накопленное с прошлого чтения (сброс между тестами).
_READER = InMemoryMetricReader(
    preferred_temporality=dict.fromkeys(
        (
            Counter,
            Histogram,
            UpDownCounter,
            ObservableCounter,
            ObservableGauge,
            ObservableUpDownCounter,
        ),
        AggregationTemporality.DELTA,
    )
)
_PROVIDERS: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def otel() -> None:
    if not _PROVIDERS:
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(tracer_provider)
        meter_provider = MeterProvider(metric_readers=[_READER])
        metrics.set_meter_provider(meter_provider)
        _PROVIDERS["tracer"] = tracer_provider
        instrument_agents()
    _EXPORTER.clear()
    _READER.get_metrics_data()  # сброс накопленного


def _dump(span: ReadableSpan) -> str:
    """Всё, что уходит из процесса: имя, атрибуты, события, ресурс."""
    return json.dumps(
        {
            "name": span.name,
            "attributes": dict(span.attributes or {}),
            "events": [(e.name, dict(e.attributes or {})) for e in span.events],
            "resource": dict(span.resource.attributes),
        },
        ensure_ascii=False,
        default=str,
    )


def _metrics() -> dict[str, list[Any]]:
    """Точки метрик с прошлого чтения (дельта): читать один раз за проверку."""
    data = _READER.get_metrics_data()
    points: dict[str, list[Any]] = {}
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                points.setdefault(m.name, []).extend(m.data.data_points)
    return points


async def test_agent_spans_have_no_content() -> None:
    gw = Gateway(
        body=_sse(
            _chunk(f"ответ {CANARY}"),
            _chunk(finish="stop"),
            {
                **_chunk(),
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )
    )
    rt = PydanticAIRuntime(
        {AGENT.name: AGENT},
        "http://litellm:4000/v1",
        "sk-platform",
        30,
        transport=httpx2.MockTransport(gw.handler),
    )
    try:
        run = rt.start(AGENT, USER, [Turn("user", f"раньше {CANARY}")], f"вопрос {CANARY}")
        text = "".join([d async for d in run.deltas()])
        await run.outcome()
    finally:
        await rt.aclose()
    assert CANARY in text  # ответ клиенту не изменился
    spans = _EXPORTER.get_finished_spans()
    assert spans, "Pydantic AI не записал спанов"
    for s in spans:
        dumped = _dump(s)
        assert CANARY not in dumped, dumped
        assert USER not in dumped, dumped
        assert "Ты ассистент" not in dumped, dumped  # инструкции агента
    assert not [s for s in spans if s.name.startswith("fastapi.")]
    names = {s.name for s in spans}
    assert any(n.startswith("chat ") for n in names), names
    usage = _metrics().get("gen_ai.client.token.usage")
    assert usage, "нет метрики токенов агента"


async def test_facade_span_and_metrics(harness: Harness, make_token: TokenFactory) -> None:
    harness.gateway.response = GatewayResponse(
        meta=ok_meta(),
        body={
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": f"Ответ {CANARY}"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )
    resp = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "chat-fast", "messages": [{"role": "user", "content": CANARY}]},
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert resp.status_code == 200, resp.text
    spans = _EXPORTER.get_finished_spans()
    # Встроенные спаны FastAPI выключены: один спан запроса, наш.
    assert not [s for s in spans if s.name.startswith("fastapi.")]
    (server,) = [s for s in spans if s.kind == trace.SpanKind.SERVER]
    assert server.name == "POST /v1/chat/completions"
    attrs = dict(server.attributes or {})
    assert attrs[telemetry.REQUEST_ID] == resp.headers["x-request-id"]
    assert attrs["http.route"] == "/v1/chat/completions"
    assert attrs["http.response.status_code"] == 200
    user = next(iter(harness.store.users.values()))
    for s in spans:
        dumped = _dump(s)
        assert CANARY not in dumped
        assert str(user.id) not in dumped
        assert "sub-1" not in dumped
    collected = _metrics()
    calls = collected["antec.model.calls"]
    assert [(dict(p.attributes)["antec.status"], p.value) for p in calls] == [("ok", 1)]
    tokens = {
        dict(p.attributes)["gen_ai.token.type"]: p.sum
        for p in collected["gen_ai.client.token.usage"]
        if dict(p.attributes).get("gen_ai.operation.name") == "chat"
        and dict(p.attributes).get("gen_ai.request.model") == "chat-fast"
    }
    assert tokens == {"input": 7, "output": 3}
    assert [p.value for p in collected["antec.service.up"]] == [1]
    durations = collected["http.server.request.duration"]
    assert {dict(p.attributes)["http.route"] for p in durations} == {"/v1/chat/completions"}


async def test_probe_has_no_span(harness: Harness) -> None:
    resp = await harness.client.get("/healthz")
    assert resp.status_code in (200, 503)
    assert _EXPORTER.get_finished_spans() == ()


async def test_span_error_records_type_only() -> None:
    with pytest.raises(ValueError, match=CANARY), telemetry.span("work"):
        raise ValueError(f"текст {CANARY}")
    with pytest.raises(ValueError, match=CANARY), telemetry.span("detached", current=False):
        raise ValueError(f"текст {CANARY}")
    for s in _EXPORTER.get_finished_spans():
        assert dict(s.attributes or {})["error.type"] == "ValueError"
        assert s.status.status_code == trace.StatusCode.ERROR
        assert not s.status.description
        assert not s.events
        assert CANARY not in _dump(s)


async def test_detached_span_does_not_become_current() -> None:
    with telemetry.span("outer") as outer, telemetry.span("inner", current=False):
        assert trace.get_current_span() is outer
