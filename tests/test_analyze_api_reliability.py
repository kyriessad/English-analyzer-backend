"""
End-to-end API tests for the analyze endpoints' reliability wiring:
Idempotency-Key replay dedup, key/fingerprint conflicts, and keyless in-flight
dedup joining the owner instead of re-running Qwen.
"""
import json
import threading
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import anyio
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models  # noqa: F401  (register ORM models on Base)
import app.main as app_main
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.observability.metrics import (
    AI_INFLIGHT_FOLLOWER_REJECT_TOTAL,
    AI_INFLIGHT_FOLLOWERS,
    AI_WAITING,
)
from app.services import auth_service
from app.services.request_reliability import (
    build_ai_request_fingerprint,
    claim_ai_request,
    reset_reliability_for_tests,
)
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


class TrackingSession:
    def __init__(self, db, events):
        self._db = db
        self._events = events

    def rollback(self):
        self._events.append("rollback")
        return self._db.rollback()

    def close(self):
        return self._db.close()

    def __getattr__(self, name):
        return getattr(self._db, name)


def tracking_get_db(events):
    def override():
        db = TrackingSession(TestingSessionLocal(), events)
        try:
            yield db
        finally:
            db.close()

    return override


class TrackingSemaphore:
    def __init__(self, capacity, events):
        self._semaphore = threading.BoundedSemaphore(capacity)
        self._events = events

    def acquire(self, *args, **kwargs):
        self._events.append("acquire")
        return self._semaphore.acquire(*args, **kwargs)

    def release(self):
        self._events.append("release")
        return self._semaphore.release()


def _metric_value(metric) -> float:
    return metric._value.get()


@asynccontextmanager
async def async_nullcontext():
    yield


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
        self.original_follower_semaphore = app_main._ai_inflight_follower_semaphore
        app_main._ai_inflight_follower_semaphore = threading.BoundedSemaphore(3)
        self.followers_before = _metric_value(AI_INFLIGHT_FOLLOWERS)
        self.follower_reject_before = _metric_value(AI_INFLIGHT_FOLLOWER_REJECT_TOTAL)
        self.ai_waiting_before = _metric_value(AI_WAITING)

    def tearDown(self):
        app_main._ai_inflight_follower_semaphore = self.original_follower_semaphore
        self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before)
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

    def test_follower_rolls_back_before_waiting_for_owner(self):
        events = []
        app.dependency_overrides[get_db] = tracking_get_db(events)
        app_main._ai_inflight_follower_semaphore = TrackingSemaphore(1, events)
        claim_ai_request(self.user_id, "k-follower-rollback", build_ai_request_fingerprint(PAYLOAD))

        def wait_for_owner(*args, **kwargs):
            events.append("wait")
            return MOCK_RESULT, None

        with (
            patch("app.main.wait_for_ai_request_record_async", side_effect=wait_for_owner) as wait,
            patch("app.main.analyze_text") as analyze,
        ):
            response = self._post(key="k-follower-rollback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["translation"], MOCK_RESULT["translation"])
        self.assertEqual(events[:4], ["rollback", "acquire", "wait", "release"])
        wait.assert_called_once()
        analyze.assert_not_called()

    def test_follower_rolls_back_before_wait_exception(self):
        events = []
        app.dependency_overrides[get_db] = tracking_get_db(events)
        app_main._ai_inflight_follower_semaphore = TrackingSemaphore(1, events)
        _, record, _ = claim_ai_request(
            self.user_id,
            "k-follower-wait-exception",
            build_ai_request_fingerprint(PAYLOAD),
        )

        def wait_for_owner(*args, **kwargs):
            events.append("wait")
            raise RuntimeError("synthetic wait failure")

        with patch("app.main.wait_for_ai_request_record_async", side_effect=wait_for_owner):
            with self.assertRaises(RuntimeError):
                self._post(key="k-follower-wait-exception")

        self.assertEqual(events[:4], ["rollback", "acquire", "wait", "release"])
        self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before)
        self.assertFalse(record.event.is_set())

    def test_owner_path_does_not_use_follower_rollback(self):
        events = []
        app.dependency_overrides[get_db] = tracking_get_db(events)
        app_main._ai_inflight_follower_semaphore = TrackingSemaphore(1, events)

        with (
            patch("app.main.check_daily_quota") as check_quota,
            patch("app.main.consume_daily_quota") as consume_quota,
            patch("app.main.async_resource_slot", side_effect=lambda *_a, **_k: async_nullcontext()) as slot,
            patch("app.main.wait_for_ai_request_record_async") as wait,
            patch("app.main.analyze_text", return_value=MOCK_RESULT) as analyze,
        ):
            response = self._post(key="k-owner-no-follower-rollback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [])
        check_quota.assert_called_once()
        consume_quota.assert_called_once()
        slot.assert_called_once()
        analyze.assert_called_once()
        wait.assert_not_called()

    def test_follower_capacity_allows_three_and_rejects_fourth_fast(self):
        # Concurrent requests must exercise follower capacity, not contend on
        # the single shared SQLite connection merely to re-read the same user.
        app.dependency_overrides[app_main.get_current_user] = lambda: User(
            id=self.user_id,
            wx_openid=f"openid-{self.user_id}",
        )
        self.addCleanup(app.dependency_overrides.pop, app_main.get_current_user, None)
        owner_started = threading.Event()
        release_owner = threading.Event()
        outcomes = {}

        def slow_analyze(**kwargs):
            owner_started.set()
            release_owner.wait(timeout=10)
            return MOCK_RESULT

        def post_named(name):
            outcomes[name] = self._post(key="k-follower-capacity")

        with (
            patch("app.main.check_daily_quota"),
            patch("app.main.consume_daily_quota"),
            patch("app.main.async_resource_slot", side_effect=lambda *_a, **_k: async_nullcontext()),
            patch("app.main.analyze_text", side_effect=slow_analyze) as analyze,
        ):
            owner = threading.Thread(target=post_named, args=("owner",))
            owner.start()
            self.assertTrue(owner_started.wait(5), "owner should reach analyzer")

            followers = [threading.Thread(target=post_named, args=(f"follower-{idx}",)) for idx in range(3)]
            for follower in followers:
                follower.start()

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if _metric_value(AI_INFLIGHT_FOLLOWERS) == self.followers_before + 3:
                    break
                time.sleep(0.005)
            self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before + 3)
            self.assertEqual(_metric_value(AI_WAITING), self.ai_waiting_before)

            started = time.perf_counter()
            extra = self._post(key="k-follower-capacity")
            elapsed = time.perf_counter() - started
            self.assertEqual(extra.status_code, 503)
            self.assertEqual(
                extra.json()["detail"],
                "\u5f53\u524d\u76f8\u540c AI \u8bf7\u6c42\u8fc7\u591a\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5",
            )
            self.assertLess(elapsed, 0.25)
            self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before + 3)
            self.assertEqual(
                _metric_value(AI_INFLIGHT_FOLLOWER_REJECT_TOTAL),
                self.follower_reject_before + 1,
            )

            release_owner.set()
            owner.join(10)
            for follower in followers:
                follower.join(10)

        self.assertFalse(owner.is_alive())
        self.assertTrue(all(not follower.is_alive() for follower in followers))
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(outcomes["owner"].status_code, 200)
        for idx in range(3):
            self.assertEqual(outcomes[f"follower-{idx}"].status_code, 200)
            self.assertEqual(outcomes[f"follower-{idx}"].json(), outcomes["owner"].json())
        self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before)

    def test_follower_timeout_releases_capacity(self):
        claim_ai_request(self.user_id, "k-follower-timeout", build_ai_request_fingerprint(PAYLOAD))

        with patch("app.main.total_timeout_deadline", side_effect=lambda _seconds: time.monotonic() + 0.03):
            response = self._post(key="k-follower-timeout")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before)

        acquired = [app_main._ai_inflight_follower_semaphore.acquire(blocking=False) for _ in range(3)]
        self.assertEqual(acquired, [True, True, True])
        for _ in acquired:
            app_main._ai_inflight_follower_semaphore.release()

    def test_concurrent_followers_never_exceed_capacity(self):
        app.dependency_overrides[app_main.get_current_user] = lambda: User(
            id=self.user_id,
            wx_openid=f"openid-{self.user_id}",
        )
        claim_ai_request(self.user_id, "k-follower-race", build_ai_request_fingerprint(PAYLOAD))
        start = threading.Barrier(9)
        release_waiters = threading.Event()
        outcomes = []
        outcomes_lock = threading.Lock()
        max_followers = 0.0
        max_lock = threading.Lock()

        async def wait_for_owner(*args, **kwargs):
            nonlocal max_followers
            with max_lock:
                max_followers = max(
                    max_followers,
                    _metric_value(AI_INFLIGHT_FOLLOWERS) - self.followers_before,
                )
            await anyio.to_thread.run_sync(release_waiters.wait, 5)
            return MOCK_RESULT, None

        def contender():
            start.wait()
            response = self._post(key="k-follower-race")
            with outcomes_lock:
                outcomes.append(response.status_code)

        try:
            with patch("app.main.wait_for_ai_request_record_async", side_effect=wait_for_owner):
                threads = [threading.Thread(target=contender) for _ in range(8)]
                for thread in threads:
                    thread.start()
                start.wait()

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with outcomes_lock:
                        rejected = outcomes.count(503)
                    if _metric_value(AI_INFLIGHT_FOLLOWERS) == self.followers_before + 3 and rejected == 5:
                        break
                    time.sleep(0.005)

                self.assertLessEqual(max_followers, 3)
                self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before + 3)
                release_waiters.set()
                for thread in threads:
                    thread.join(10)
        finally:
            release_waiters.set()
            app.dependency_overrides.pop(app_main.get_current_user, None)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(outcomes.count(200), 3)
        self.assertEqual(outcomes.count(503), 5)
        self.assertEqual(_metric_value(AI_INFLIGHT_FOLLOWERS), self.followers_before)

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

    def test_stream_follower_rolls_back_before_waiting_and_replays(self):
        events = []
        app.dependency_overrides[get_db] = tracking_get_db(events)
        app_main._ai_inflight_follower_semaphore = TrackingSemaphore(1, events)
        claim_ai_request(self.user_id, "k-stream-follower-rollback", build_ai_request_fingerprint(PAYLOAD))

        def wait_for_owner(*args, **kwargs):
            events.append("wait")
            return MOCK_RESULT, None

        with (
            patch("app.main.wait_for_ai_request_record_async", side_effect=wait_for_owner) as wait,
            patch("app.main.analyze_text_streaming") as stream,
        ):
            response = self.client.post(
                "/api/analyze-english/stream",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": "k-stream-follower-rollback",
                },
                json=PAYLOAD,
            )

        self.assertEqual(response.status_code, 200)
        lines = [json.loads(ln) for ln in response.text.splitlines()]
        self.assertEqual([ln["type"] for ln in lines], ["start", "final", "done"])
        self.assertEqual(lines[1]["data"]["translation"], MOCK_RESULT["translation"])
        self.assertEqual(events[:4], ["rollback", "acquire", "wait", "release"])
        wait.assert_called_once()
        stream.assert_not_called()

    def test_stream_distinct_keys_start_independent_generations_for_same_inflight_payload(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        call_count = 0
        outcomes = {}

        def fake_stream(**kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                call_number = call_count
            if call_number == 1:
                first_started.set()
                release_first.wait(timeout=10)
            else:
                second_started.set()
            for seq, text in enumerate(("新", "一轮", "生成"), start=1):
                yield ("delta", "meaning", text, seq, 1)
            result = dict(MOCK_RESULT)
            result["translation"] = f"generation-{call_number}"
            yield ("final", result, 1)

        def post_stream(name, key):
            outcomes[name] = self.client.post(
                "/api/analyze-english/stream",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": key,
                },
                json={**PAYLOAD, "forceRefresh": True},
            )

        with (
            patch("app.main.enforce_resource_rate_limit"),
            patch("app.main.check_daily_quota"),
            patch("app.main.consume_daily_quota"),
            patch("app.main.async_resource_slot", side_effect=lambda *_a, **_k: async_nullcontext()),
            patch("app.main.analyze_text_streaming", side_effect=fake_stream) as mock,
        ):
            first = threading.Thread(target=post_stream, args=("A", "generation-A"))
            second = threading.Thread(target=post_stream, args=("B", "generation-B"))
            first.start()
            self.assertTrue(first_started.wait(5), "request A should enter generation")
            second.start()
            try:
                self.assertTrue(
                    second_started.wait(5),
                    "a different Idempotency-Key must start request B's own generation",
                )
            finally:
                release_first.set()
            first.join(10)
            second.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(mock.call_count, 2)
        for name in ("A", "B"):
            self.assertEqual(outcomes[name].status_code, 200)
            events = [json.loads(line) for line in outcomes[name].text.splitlines()]
            self.assertEqual(
                [event["type"] for event in events],
                ["start", "delta", "delta", "delta", "final", "done"],
            )

    def test_stream_serializes_delta_field_reset_and_attempt_metadata(self):
        def fake_stream(**kwargs):
            yield ("delta", "meaning", "这里", 1, 1)
            yield ("field", "meaning", "这里表示愿意", 1)
            yield ("reset", 2)
            yield ("delta", "meaning", "这里通常", 2, 2)
            yield ("field", "meaning", "这里通常表示愿意", 2)
            yield ("final", dict(MOCK_RESULT), 2)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": "k-stream-events",
        }
        with patch("app.main.analyze_text_streaming", side_effect=fake_stream) as mock:
            response = self.client.post(
                "/api/analyze-english/stream",
                headers=headers,
                json=PAYLOAD,
            )

        self.assertEqual(response.status_code, 200)
        lines = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(
            [line["type"] for line in lines],
            ["start", "delta", "field", "reset", "delta", "field", "final", "done"],
        )
        self.assertEqual(lines[1], {
            "type": "delta",
            "field": "meaning",
            "text": "这里",
            "seq": 1,
            "attempt": 1,
        })
        self.assertEqual(lines[3], {"type": "reset", "attempt": 2})
        self.assertEqual(lines[4]["seq"], 2)
        self.assertEqual(lines[4]["attempt"], 2)
        self.assertEqual(lines[-2]["attempt"], 2)
        self.assertEqual(lines[-2]["data"]["translation"], "渴望")
        mock.assert_called_once()

    def test_incomplete_stream_replays_error_directly_without_new_generation(self):
        def incomplete_stream(**kwargs):
            yield ("delta", "meaning", "半截临时内容", 1, 1)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": "k-stream-incomplete",
        }
        with patch("app.main.analyze_text_streaming", side_effect=incomplete_stream):
            streamed = self.client.post(
                "/api/analyze-english/stream",
                headers=headers,
                json=PAYLOAD,
            )

        stream_events = [json.loads(line) for line in streamed.text.splitlines()]
        self.assertEqual([event["type"] for event in stream_events], ["start", "delta"])

        # The frontend's direct fallback reuses the same key. The completed
        # stream-error record must be replayed instead of running Qwen again.
        with patch("app.main.analyze_text") as direct_analyze:
            replay = self._post(key="k-stream-incomplete")

        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.json()["ok"])
        direct_analyze.assert_not_called()
