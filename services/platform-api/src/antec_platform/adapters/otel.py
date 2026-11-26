"""SDK OpenTelemetry и экспорт OTLP/HTTP в otel-collector (ADR-0013).

Трейсы и метрики; журналы остаются в stdout (JSON, logs.py). Ресурс: имя и версия сервиса, без
имени хоста и других сведений о машине. Сбой экспорта не влияет на запросы: SDK отбрасывает
данные после повторов.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_EXPORT_TIMEOUT_SECONDS = 10.0
_METRIC_INTERVAL_MS = 30_000


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def setup(endpoint: str, service_name: str) -> Telemetry:
    """Глобальные провайдеры трейсов и метрик; endpoint вида http://otel-collector:4318."""
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": metadata.version("antec-platform"),
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=_EXPORT_TIMEOUT_SECONDS)
        )
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{endpoint}/v1/metrics", timeout=_EXPORT_TIMEOUT_SECONDS
                ),
                export_interval_millis=_METRIC_INTERVAL_MS,
            )
        ],
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    return Telemetry(tracer_provider, meter_provider)
