from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from app.observability.errors import classify_exception
from app.observability.logging import log_event
from app.observability.metrics import (
    AI_REQUEST_DURATION_SECONDS,
    AI_REQUESTS_TOTAL,
    COMPONENT_OPERATION_DURATION_SECONDS,
    COMPONENT_OPERATIONS_TOTAL,
    DB_OPERATION_DURATION_SECONDS,
    DB_OPERATIONS_TOTAL,
    TTS_REQUEST_DURATION_SECONDS,
    TTS_REQUESTS_TOTAL,
)
from app.observability.tracing import start_span

logger = logging.getLogger(__name__)


def _observe_metric(component: str, operation: str, result: str, duration: float) -> None:
    if component == "database":
        DB_OPERATIONS_TOTAL.labels(operation=operation, result=result).inc()
        DB_OPERATION_DURATION_SECONDS.labels(operation=operation, result=result).observe(duration)
    elif component == "ai":
        AI_REQUESTS_TOTAL.labels(operation=operation, result=result).inc()
        AI_REQUEST_DURATION_SECONDS.labels(operation=operation, result=result).observe(duration)
    elif component == "tts":
        TTS_REQUESTS_TOTAL.labels(operation=operation, result=result).inc()
        TTS_REQUEST_DURATION_SECONDS.labels(operation=operation, result=result).observe(duration)
    else:
        COMPONENT_OPERATIONS_TOTAL.labels(component=component, operation=operation, result=result).inc()
        COMPONENT_OPERATION_DURATION_SECONDS.labels(component=component, operation=operation, result=result).observe(duration)


def record_operation_result(
    component: str,
    operation: str,
    result: str,
    duration: float,
    *,
    attributes: dict[str, object] | None = None,
) -> None:
    """Record a completed operation when success/error is represented in data."""
    _observe_metric(component, operation, result, duration)
    fields = {
        "component": component,
        "operation": operation,
        "result": result,
        "duration_ms": round(duration * 1000, 3),
        **(attributes or {}),
    }
    log_event(
        logger,
        logging.INFO if result in {"success", "hit", "miss", "generated"} else logging.WARNING,
        f"{component}_operation_finished",
        **fields,
    )


@contextmanager
def observed_operation(
    component: str,
    operation: str,
    *,
    span_name: str | None = None,
    attributes: dict[str, object] | None = None,
) -> Iterator[None]:
    started = time.perf_counter()
    result = "success"
    error_type = None
    attrs = {"component": component, "operation": operation, **(attributes or {})}
    with start_span(span_name or f"{component}.{operation}", attrs) as span:
        try:
            yield
        except BaseException as exc:
            result = "error"
            error_type = classify_exception(exc)
            if error_type == "timeout":
                result = "timeout"
            elif error_type == "cancelled":
                result = "cancelled"
            if span is not None:
                span.record_exception(exc)
                span.set_attribute("error_type", error_type)
            raise
        finally:
            duration = time.perf_counter() - started
            _observe_metric(component, operation, result, duration)
            if span is not None:
                span.set_attribute("result", result)
                span.set_attribute("duration_ms", round(duration * 1000, 3))
                if error_type:
                    span.set_attribute("error_type", error_type)
            log_event(
                logger,
                logging.INFO if result == "success" else logging.WARNING,
                f"{component}_operation_finished",
                component=component,
                operation=operation,
                result=result,
                error_type=error_type,
                duration_ms=round(duration * 1000, 3),
            )
