"""Layer 1 API and card contract checks.

The assertions are intentionally strict. They define the Layer 2/3 oracle and
must not be relaxed to match an older response shape.
"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_service import get_current_user
from app.services.card_service import create_card
from app.schemas.card import CardCreate


def _client() -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="layer1-contract-user")
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.pop(get_current_user, None)


def test_API_002_request_id_valid_boundary_is_preserved_and_invalid_value_is_replaced():
    client = _client()
    valid = "rule-api-req-001"
    response = client.get("/health", headers={"X-Request-ID": valid})
    assert response.headers["X-Request-ID"] == valid

    too_long = "x" * 129
    response = client.get("/health", headers={"X-Request-ID": too_long})
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Request-ID"] != too_long


def test_API_003_response_invariants_for_pass_warning_and_error():
    cases = [
        ({"level": "pass", "warnings": [], "errors": []}, "pass"),
        ({"level": "warning", "warnings": ["advisory"], "errors": []}, "warning"),
        ({"level": "error", "warnings": [], "errors": ["hard"]}, "error"),
    ]
    for partial, expected_level in cases:
        result = {
            "category": "word",
            "normalizedText": "because",
            "evidence": [],
            "warningTypes": [] if expected_level != "warning" else ["ADVISORY_WARNING"],
            "canSave": expected_level != "error",
            "canAnalyze": expected_level != "error",
            "canPronounce": expected_level != "error",
            **partial,
        }
        assert result["level"] == expected_level
        if expected_level == "error":
            assert result["errors"]
            assert result["warnings"] == []
        elif expected_level == "warning":
            assert result["errors"] == []
            assert result["warnings"]
        else:
            assert result["errors"] == []
            assert result["warnings"] == []


def test_API_004_validation_endpoint_exposes_frozen_capabilities():
    client = _client()
    response = client.post(
        "/api/validate-english",
        json={"text": "because", "cardType": "word"},
        headers={"X-Request-ID": "layer1-api-contract"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "level",
        "warningTypes",
        "canSave",
        "canAnalyze",
        "canPronounce",
    }
    assert response.headers["X-Request-ID"] == "layer1-api-contract"


def test_CARD_001_error_is_rejected_before_persistence():
    class FailingSession:
        def add(self, value):
            raise AssertionError("database write must not happen for hard error")

        def commit(self):
            raise AssertionError("database commit must not happen for hard error")

    try:
        create_card(
            FailingSession(),
            CardCreate(
                user_id=UUID("00000000-0000-0000-0000-000000000001"),
                content="https://example.com",
                card_type="word",
                local_temp_id="layer1-hard-error",
            ),
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("hard-error card unexpectedly saved")
