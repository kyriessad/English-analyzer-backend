from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_service import get_current_user


def _client() -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="validation-test-user")
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.pop(get_current_user, None)


def test_validation_endpoint_returns_normalized_pass_without_ai() -> None:
    client = _client()

    with patch("app.main.analyze_text", side_effect=AssertionError("AI must not run")):
        response = client.post(
            "/api/validate-english",
            json={"text": "  absolutely， yes  ", "cardType": "auto"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "pass"
    assert body["normalizedText"] == "absolutely, yes"
    assert body["category"] == "phrase"
    assert body["warnings"] == []
    assert body["errors"] == []
    assert isinstance(body["evidence"], list)


def test_validation_endpoint_returns_actionable_invalid_result() -> None:
    client = _client()

    response = client.post(
        "/api/validate-english",
        json={"text": "我的", "cardType": "单词"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "error"
    assert body["category"] == "unknown"
    assert "不能包含中文字符" in " ".join(body["errors"])
    assert body["warnings"] == []


def test_validation_endpoint_keeps_multiple_warnings_and_evidence() -> None:
    client = _client()
    fake_result = {
        "level": "warning",
        "category": "sentence",
        "normalizedText": "This are fine???",
        "warnings": [
            "Harper grammar: The verb may not agree with the subject.",
            "内容中有连续或混合标点，建议确认是否为有意输入。",
            "这段内容看起来更像句子。",
        ],
        "errors": [],
        "evidence": [
            {
                "source": "harper",
                "type": "grammar",
                "result": "lint",
                "polarity": "warning",
                "message": "The verb may not agree with the subject.",
                "offset": 5,
                "length": 3,
                "replacements": ["is"],
            }
        ],
    }

    with patch("app.main.validate_english", return_value=fake_result):
        response = client.post(
            "/api/validate-english",
            json={"text": "This are fine???", "cardType": "单词"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "warning"
    assert len(body["warnings"]) == 3
    assert body["evidence"][0]["type"] == "grammar"
