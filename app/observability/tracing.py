from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.core.config import settings

logger = logging.getLogger(__name__)
_configured = False
_trace_writer = None

_backend_root = Path(__file__).resolve().parents[2]
_trace_path = Path(
    os.getenv("TRACE_LOG_PATH", str(_backend_root / "logs" / "traces.jsonl"))
).expanduser().resolve()
_trace_max_bytes = 50 * 1024 * 1024
_trace_retention_days = 7


class _RotatingTraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._stream = None

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file() and self.path.stat().st_size >= _trace_max_bytes:
            rotated = self.path.with_name(
                f"{self.path.name}."
                f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"
            )
            self.path.replace(rotated)
        cutoff = datetime.now(timezone.utc) - timedelta(days=_trace_retention_days)
        for candidate in self.path.parent.glob(f"{self.path.name}.*"):
            try:
                if datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc) < cutoff:
                    candidate.unlink()
            except OSError:
                continue
        self._stream = self.path.open("a", encoding="utf-8")

    def write(self, value: str) -> int:
        with self._lock:
            if self._stream is None or self._stream.closed:
                self._open()
            written = self._stream.write(value)
            self._stream.flush()
            if self._stream.tell() >= _trace_max_bytes:
                self._stream.close()
                self._stream = None
            return written

    def flush(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

try:
    from opentelemetry import context as otel_context
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
    return trace is not None and otel_context is not None


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
    global _trace_writer
    _trace_writer = _RotatingTraceWriter(_trace_path)
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=_trace_writer)))
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


def _current_context() -> contextvars.Context | None:
    """Return the exact asyncio Context the caller runs in, or None.

    Used to detach an OTel span token inside the same Context object it was
    attached to. Outside a running loop (e.g. the bridge's sync worker thread)
    there is no task context to pin, so the caller falls back to a plain detach.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None  # no running event loop in this thread
    if task is None:
        return None
    return task.get_context()


@contextmanager
def start_span(name: str, attributes: dict[str, object] | None = None) -> Iterator[object | None]:
    tracer = get_tracer()
    if tracer is None or not settings.tracing_enabled:
        yield None
        return
    span = tracer.start_span(name)
    for key, value in (attributes or {}).items():
        if value is not None:
            span.set_attribute(key, value)
    token = None
    enter_ctx: contextvars.Context | None = None
    try:
        # start_as_current_span would detach in whatever context __exit__ runs
        # in. asyncio finalizes a cancelled async generator by calling aclose()
        # from a *copied* context, which makes ContextVar.reset(token) raise
        # "Token was created in a different Context" (logged as a Failed to
        # detach context ERROR). Pinning the exact context and detaching inside
        # it keeps the attach/detach pair on one Context object.
        token = otel_context.attach(trace.set_span_in_context(span))
        enter_ctx = _current_context()
        yield span
    except Exception as exc:
        if span.is_recording():
            span.record_exception(exc)
            span.set_status(
                trace.Status(
                    trace.StatusCode.ERROR,
                    description=f"{type(exc).__name__}: {exc}",
                )
            )
        raise
    finally:
        if token is not None:
            if enter_ctx is not None:
                try:
                    # If __exit__ runs inside enter_ctx already, Context.run()
                    # raises RuntimeError and the plain detach below is correct.
                    enter_ctx.run(otel_context.detach, token)
                except RuntimeError:
                    otel_context.detach(token)
            else:
                otel_context.detach(token)
        span.end()
