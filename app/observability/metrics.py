from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ("method", "route", "status", "result"),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being processed.",
    ("method", "route"),
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route", "status", "result"),
)

AI_REQUESTS_TOTAL = Counter(
    "ai_requests_total",
    "AI analysis requests by result.",
    ("operation", "result"),
)
AI_REQUEST_DURATION_SECONDS = Histogram(
    "ai_request_duration_seconds",
    "AI analysis duration in seconds.",
    ("operation", "result"),
)
AI_CACHE_EVENTS_TOTAL = Counter(
    "ai_cache_events_total",
    "AI cache hit/miss events.",
    ("operation", "result"),
)

TTS_REQUESTS_TOTAL = Counter(
    "tts_requests_total",
    "TTS requests by result.",
    ("operation", "result"),
)
TTS_REQUEST_DURATION_SECONDS = Histogram(
    "tts_request_duration_seconds",
    "TTS request duration in seconds.",
    ("operation", "result"),
)
TTS_CACHE_EVENTS_TOTAL = Counter(
    "tts_cache_events_total",
    "TTS wav cache hit/miss events.",
    ("operation", "result"),
)

DB_OPERATIONS_TOTAL = Counter(
    "db_operations_total",
    "Database operations by coarse business area.",
    ("operation", "result"),
)
DB_OPERATION_DURATION_SECONDS = Histogram(
    "db_operation_duration_seconds",
    "Database operation duration in seconds.",
    ("operation", "result"),
)

COMPONENT_OPERATIONS_TOTAL = Counter(
    "component_operations_total",
    "External or derived component operations.",
    ("component", "operation", "result"),
)
COMPONENT_OPERATION_DURATION_SECONDS = Histogram(
    "component_operation_duration_seconds",
    "External or derived component operation duration in seconds.",
    ("component", "operation", "result"),
)


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
