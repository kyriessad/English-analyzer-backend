import json
import logging

from fastapi.testclient import TestClient

from app.main import app
from app.observability.context import reset_request_id, set_request_id
from app.observability.logging import JsonFormatter
from app.observability.tracing import start_span, tracing_available


def test_request_id_header_and_http_metrics_are_exposed():
    client = TestClient(app)

    response = client.get("/health", headers={"X-Request-ID": "obs-test-req-1"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "obs-test-req-1"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text
    assert "http_requests_total" in body
    assert 'route="/health"' in body
    assert "http_request_duration_seconds" in body


def test_structured_log_contains_request_and_trace_ids():
    formatter = JsonFormatter()
    token = set_request_id("obs-test-log-1")
    try:
        with start_span("test.structured_log"):
            record = logging.LogRecord(
                name="test.observability",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="observability_test_event",
                args=(),
                exc_info=None,
            )
            record.event = "observability_test_event"
            record.observability = {"component": "test", "operation": "structured_log"}
            payload = json.loads(formatter.format(record))
    finally:
        reset_request_id(token)

    assert payload["event"] == "observability_test_event"
    assert payload["request_id"] == "obs-test-log-1"
    assert payload["component"] == "test"
    assert payload["operation"] == "structured_log"
    if tracing_available():
        assert payload.get("trace_id")
        assert payload.get("span_id")
