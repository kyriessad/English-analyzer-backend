"""
End-to-end API tests for the analyze endpoints' reliability wiring:
Idempotency-Key replay dedup, key/fingerprint conflicts, and keyless in-flight
dedup joining the owner instead of re-running Qwen.
"""
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models  # noqa: F401  (register ORM models on Base)
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import auth_service
from app.services.request_reliability import reset_reliability_for_tests
from app.services.security import rate_limiter


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


MOCK_RESULT = {
    "ok": True,
    "level": "pass",
    "category": "word",
    "normalizedText": "crave",
    "translation": "渴望",
    "understanding": "to want something very much",
    "warnings": [],
    "errors": [],
    "provider": "ollama",
    "cacheHit": False,
    "exampleSentence": "She craves quiet mornings.",
    "exampleTranslation": "她渴望安静的早晨。",
    "synonyms": [],
    "similarPhrases": [],
    "expressionType": "literal",
    "alternativeMeanings": [],
    "usageScenario": "",
    "dialogue": {"english": [], "chinese": []},
}

MOCK_FAILURE = {
    "ok": False,
    "level": "failed",
    "category": "unknown",
    "normalizedText": "crave",
    "warnings": [],
    "errors": ["分析服务暂时不可用，请稍后重试"],
    "provider": None,
}

PAYLOAD = {"text": "crave", "cardType": "auto", "targetLang": "zh", "forceRefresh": False}
OTHER_PAYLOAD = {"text": "different text", "cardType": "auto", "targetLang": "zh", "forceRefresh": False}


class AnalyzeReliabilityApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        reset_reliability_for_tests()
        rate_limiter.reset()
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="analyze-api-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.user_id = uuid4()
        with TestingSessionLocal() as db:
            db.add(User(id=self.user_id, wx_openid=f"openid-{self.user_id}"))
            db.commit()
        self.token = auth_service.create_access_token(self.user_id)
        self.client = TestClient(app)

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)
        reset_reliability_for_tests()
        rate_limiter.reset()

    def _post(self, payload=None, key=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post(
            "/api/analyze-english",
            headers=headers,
            json=payload or PAYLOAD,
        )

    # ------------------------------------------------------------------
    # Non-streaming endpoint
    # ------------------------------------------------------------------

    def test_idempotency_replay_returns_same_result_without_rerun(self):
        with patch("app.main.analyze_text", return_value=MOCK_RESULT) as mock:
            r1 = self._post(key="k-replay")
            r2 = self._post(key="k-replay")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["translation"], "渴望")
        self.assertEqual(r1.json(), r2.json())
        mock.assert_called_once()

    def test_idempotency_replay_propagates_owner_failure(self):
        with patch("app.main.analyze_text", return_value=MOCK_FAILURE) as mock:
            r1 = self._post(key="k-fail")
            r2 = self._post(key="k-fail")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r1.json()["ok"])
        self.assertEqual(r1.json(), r2.json())
        mock.assert_called_once()

    def test_same_key_different_payload_is_conflict(self):
        with patch("app.main.analyze_text", return_value=MOCK_RESULT):
            r1 = self._post(key="k-conflict")
            self.assertEqual(r1.status_code, 200)
        headers = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": "k-conflict"}
        r2 = self.client.post("/api/analyze-english", headers=headers, json=OTHER_PAYLOAD)
        self.assertEqual(r2.status_code, 409)

    def test_keyless_inflight_dedup_joins_owner(self):
        owner_blocked = threading.Event()
        release = threading.Event()
        outcomes = {}

        def slow_analyze(**kwargs):
            owner_blocked.set()
            release.wait(timeout=15)
            return MOCK_RESULT

        with patch("app.main.analyze_text", side_effect=slow_analyze) as mock:
            def run_owner():
                outcomes["owner"] = self._post()

            owner_thread = threading.Thread(target=run_owner)
            owner_thread.start()
            self.assertTrue(owner_blocked.wait(10), "owner should reach the analyzer")

            dup_outcome = {}

            def run_dup():
                dup_outcome["resp"] = self._post()

            dup_thread = threading.Thread(target=run_dup)
            dup_thread.start()
            time.sleep(0.4)  # give the duplicate time to start waiting
            release.set()

            owner_thread.join(15)
            dup_thread.join(15)

        self.assertFalse(owner_thread.is_alive(), "owner should have finished")
        self.assertFalse(dup_thread.is_alive(), "duplicate should have finished")
        self.assertEqual(mock.call_count, 1, "only the owner should run Qwen")
        self.assertEqual(outcomes["owner"].status_code, 200)
        self.assertEqual(dup_outcome["resp"].status_code, 200)
        self.assertEqual(outcomes["owner"].json(), dup_outcome["resp"].json())

    # ------------------------------------------------------------------
    # Streaming endpoint
    # ------------------------------------------------------------------

    def test_stream_idempotency_replay_reuses_owner_result(self):
        def fake_stream(**kwargs):
            yield ("final", dict(MOCK_RESULT))

        headers = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": "k-stream"}
        with patch("app.main.analyze_text_streaming", side_effect=fake_stream) as mock:
            r1 = self.client.post("/api/analyze-english/stream", headers=headers, json=PAYLOAD)
            r2 = self.client.post("/api/analyze-english/stream", headers=headers, json=PAYLOAD)

        lines1 = [json.loads(ln) for ln in r1.text.splitlines()]
        lines2 = [json.loads(ln) for ln in r2.text.splitlines()]
        self.assertEqual([ln["type"] for ln in lines1], ["start", "final", "done"])
        self.assertEqual([ln["type"] for ln in lines2], ["start", "final", "done"])
        self.assertEqual(lines1[1]["data"], lines2[1]["data"])
        self.assertTrue(lines1[1]["data"]["ok"])
        mock.assert_called_once()
