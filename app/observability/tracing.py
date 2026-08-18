from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from app.core.config import settings

logger = logging.getLogger(__name__)
_configured = False

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
except Exception:  # pragma: no cover - import failure is only for incomplete env setup
    trace = None
    Resource = None
    TracerProvider = None
    ConsoleSpanExporter = None
    SimpleSpanProcessor = None


def tracing_available() -> bool:
    return trace is not None


def configure_tracing() -> None:
    global _configured
    if _configured or not settings.tracing_enabled:
        return
    if not tracing_available():
        logger.warning("OpenTelemetry SDK is not installed; tracing disabled")
        return

    provider = TracerProvider(
        resource=Resource.create({"service.name": "english-analyzer-backend"})
    )
    # Synchronous console export is sufficient for this single-process setup and
    # avoids background writes to a closed stdout during tests or short scripts.
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer():
    if not tracing_available():
        return None
    return trace.get_tracer("english-analyzer-backend")


def current_trace_ids() -> tuple[str | None, str | None]:
    if not tracing_available():
        return None, None
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context or not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


@contextmanager
def start_span(name: str, attributes: dict[str, object] | None = None) -> Iterator[object | None]:
    tracer = get_tracer()
    if tracer is None or not settings.tracing_enabled:
        yield None
        return
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        yield span
