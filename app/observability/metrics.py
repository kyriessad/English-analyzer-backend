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
AI_REQUEST_EVENTS_TOTAL = Counter(
    "ai_request_events_total",
    "AI request reliability events.",
    ("operation", "event", "result"),
)
AI_CACHE_EVENTS_TOTAL = Counter(
    "ai_cache_events_total",
    "AI cache hit/miss events.",
    ("operation", "result"),
)
AI_ACTIVE = Gauge(
    "ai_active",
    "AI requests currently holding the execution slot.",
)
AI_WAITING = Gauge(
    "ai_waiting",
    "AI requests currently waiting for the execution slot.",
)
AI_SLOT_WAIT_SECONDS = Histogram(
    "ai_slot_wait_seconds",
    "Time spent waiting for the AI execution slot.",
)
AI_SLOT_TIMEOUT_TOTAL = Counter(
    "ai_slot_timeout_total",
    "AI requests rejected after timing out while waiting for the execution slot.",
)
AI_QUEUE_FULL_REJECT_TOTAL = Counter(
    "ai_queue_full_reject_total",
    "AI requests rejected immediately because the waiting capacity is full.",
)
AI_INFLIGHT_FOLLOWERS = Gauge(
    "ai_inflight_followers",
    "AI duplicate requests currently waiting for an in-flight owner result.",
)
AI_INFLIGHT_FOLLOWER_REJECT_TOTAL = Counter(
    "ai_inflight_follower_reject_total",
    "AI duplicate requests rejected because the in-flight follower capacity is full.",
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
TTS_ACTIVE = Gauge(
    "tts_active",
    "TTS requests currently holding the execution slot.",
)
TTS_WAITING = Gauge(
    "tts_waiting",
    "TTS requests currently waiting for the execution slot.",
)
TTS_SLOT_WAIT_SECONDS = Histogram(
    "tts_slot_wait_seconds",
    "Time spent waiting for the TTS execution slot.",
)
TTS_SLOT_TIMEOUT_TOTAL = Counter(
    "tts_slot_timeout_total",
    "TTS requests rejected after timing out while waiting for the execution slot.",
)
TTS_QUEUE_FULL_REJECT_TOTAL = Counter(
    "tts_queue_full_reject_total",
    "TTS requests rejected immediately because the waiting capacity is full.",
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
DB_POOL_TIMEOUT_TOTAL = Counter(
    "db_pool_timeout_total",
    "Database connection checkouts rejected after the SQLAlchemy pool timeout.",
)
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "Database connections currently checked out from the SQLAlchemy pool.",
)
DB_POOL_OVERFLOW = Gauge(
    "db_pool_overflow",
    "Database connections currently above the SQLAlchemy pool's base size.",
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


def db_pool_checked_out_value(pool) -> int:
    checked_out = getattr(pool, "checkedout", None)
    return max(0, int(checked_out())) if callable(checked_out) else 0


def db_pool_overflow_value(pool) -> int:
    overflow = getattr(pool, "overflow", None)
    return max(0, int(overflow())) if callable(overflow) else 0


def bind_db_pool_metrics(pool) -> None:
    """Read gauges from the actual SQLAlchemy Pool at Prometheus collection time."""
    DB_POOL_CHECKED_OUT.set_function(lambda: db_pool_checked_out_value(pool))
    DB_POOL_OVERFLOW.set_function(lambda: db_pool_overflow_value(pool))


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
