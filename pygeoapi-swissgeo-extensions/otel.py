import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from settings import get_settings
from starlette.applications import Starlette

_resource = Resource.create({"service.name": "service-oa-features"})


def _get_providers() -> tuple[LoggerProvider | None, TracerProvider | None]:
  settings = get_settings()

  if settings.otel_sdk_disabled:
    return None, None

  log_provider = LoggerProvider(resource=_resource)
  set_logger_provider(log_provider)

  trace_provider = TracerProvider(resource=_resource)
  trace.set_tracer_provider(trace_provider)

  return log_provider, trace_provider


def _setup_exporters(
  log_provider: LoggerProvider | None,
  trace_provider: TracerProvider | None,
) -> None:
  settings = get_settings()

  if settings.otel_sdk_disabled or not settings.otel_enable_otlp_exporter:
    return

  if log_provider is not None:
    log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(**settings.otlp_kwargs)))

  if trace_provider is not None:
    trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**settings.otlp_kwargs)))


def _setup_metrics() -> MeterProvider | None:
  settings = get_settings()

  if settings.otel_sdk_disabled or not settings.otel_enable_metrics or not settings.otel_enable_otlp_exporter:
    return None

  meter_provider = MeterProvider(
    metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(**settings.otlp_kwargs))],
    resource=_resource,
  )
  metrics.set_meter_provider(meter_provider)

  return meter_provider


# ------------------------------------------------------------------------------
# NOTE: Import-time setup is intentional.
#
# This allows uvicorn's logging.dictConfig() to resolve:
#
#   handlers:
#     otel:
#       (): otel.get_otel_handler
#
# At that point, get_otel_handler() must be importable and must already have access
# to an initialized LoggerProvider.

log_provider, trace_provider = _get_providers()

_setup_exporters(log_provider, trace_provider)

meter_provider = _setup_metrics()


def get_otel_handler() -> logging.Handler:
  """Get the OTEL logging Handler"""
  settings = get_settings()

  if settings.otel_sdk_disabled:
    raise ValueError("Cannot use OTEL handler in logging configuration when OTEL_SDK_DISABLE is true")
  if log_provider is None:
    raise ValueError("OTEL log provider is not available")

  return LoggingHandler(logger_provider=log_provider)


def initialize_instrumentation(app: Starlette) -> None:
  """Initialize OTEL instrumentation for the Starlette app."""
  settings = get_settings()

  if settings.otel_sdk_disabled:
    return

  if settings.otel_enable_asgi:
    app.add_middleware(OpenTelemetryMiddleware)
  if settings.otel_enable_psycopg2:
    Psycopg2Instrumentor().instrument(enable_commenter=True)


def shutdown_otel() -> None:
  """Flush and shutdown OTEL providers/processors on application shutdown."""
  if trace_provider is not None:
    trace_provider.shutdown()

  if log_provider is not None:
    log_provider.shutdown()

  if meter_provider is not None:
    meter_provider.shutdown()
