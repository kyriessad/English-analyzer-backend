"""
Unit tests for app.services.request_reliability reliability primitives:
claim/finish/wait, Idempotency-Key replay, in-flight dedup, deadline waits,
client cancellation and generation-attempt accounting.
"""
import time
import unittest
from uuid import uuid4

import anyio

from app.services.request_reliability import (
    ClientCancelledError,
    IdempotencyKeyReuseError,
    build_ai_request_fingerprint,
    claim_ai_request,
    finish_ai_request,
    reset_reliability_for_tests,
    touch_generation_attempt,
    user_message_for,
    wait_for_ai_request_record,
    wait_for_ai_request_record_async,
)


class ClaimAndFinishTest(unittest.TestCase):
    def setUp(self):
        reset_reliability_for_tests()

    def tearDown(self):
        reset_reliability_for_tests()

    def test_new_key_is_owner_and_creates_record(self):
        key, record, is_owner = claim_ai_request(
            "k1", build_ai_request_fingerprint({"text": "crave"})
        )
        self.assertEqual(key, "k1")
        self.assertIsNotNone(record)
        self.assertTrue(is_owner)

    def test_replay_same_key_same_payload_joins_not_reruns(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, is_owner = claim_ai_request("k1", fp)
        self.assertTrue(is_owner)
        key2, record2, is_owner2 = claim_ai_request("k1", fp)
        self.assertIs(record2, record)
        self.assertFalse(is_owner2)

    def test_key_reuse_for_different_payload_raises(self):
        claim_ai_request("k1", build_ai_request_fingerprint({"text": "crave"}))
        with self.assertRaises(IdempotencyKeyReuseError):
            claim_ai_request("k1", build_ai_request_fingerprint({"text": "different"}))

    def test_keyless_inflight_joins_by_fingerprint(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        _, record, is_owner = claim_ai_request(None, fp)
        self.assertTrue(is_owner)
        _, record2, is_owner2 = claim_ai_request(None, fp)
        self.assertIs(record2, record)
        self.assertFalse(is_owner2)

    def test_distinct_key_is_new_owner_even_for_same_inflight_fingerprint(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        _, owner_record, is_owner = claim_ai_request("k-owner", fp)
        self.assertTrue(is_owner)
        key, record, is_owner2 = claim_ai_request("k-dup", fp)
        self.assertEqual(key, "k-dup")
        self.assertIsNot(record, owner_record)
        self.assertTrue(is_owner2)

    def test_finish_clears_inflight_and_stores_result(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, is_owner = claim_ai_request(None, fp)
        self.assertTrue(is_owner)
        result = {"ok": True, "translation": "渴望"}
        finish_ai_request(key, record, result=result)
        # A new keyless request for the same payload now owns (no stale inflight).
        _, record2, is_owner2 = claim_ai_request(None, fp)
        self.assertTrue(is_owner2)
        self.assertIsNot(record2, record)
        self.assertEqual(record.result, result)

    def test_same_key_isolated_between_users(self):
        fp = build_ai_request_fingerprint({"text": "same"})
        user_a = uuid4()
        user_b = uuid4()
        _, record_a, owner_a = claim_ai_request(user_a, "same-key", fp)
        _, record_b, owner_b = claim_ai_request(user_b, "same-key", fp)
        self.assertTrue(owner_a)
        self.assertTrue(owner_b)
        self.assertIsNot(record_a, record_b)

    def test_same_fingerprint_isolated_between_users(self):
        fp = build_ai_request_fingerprint({"text": "same"})
        user_a = uuid4()
        user_b = uuid4()
        _, record_a, owner_a = claim_ai_request(user_a, None, fp)
        _, record_b, owner_b = claim_ai_request(user_b, None, fp)
        self.assertTrue(owner_a)
        self.assertTrue(owner_b)
        self.assertIsNot(record_a, record_b)

    def test_same_user_still_replays_same_key(self):
        fp = build_ai_request_fingerprint({"text": "same"})
        user_id = uuid4()
        _, record, owner = claim_ai_request(user_id, "same-key", fp)
        _, replay, replay_owner = claim_ai_request(user_id, "same-key", fp)
        self.assertTrue(owner)
        self.assertFalse(replay_owner)
        self.assertIs(record, replay)


class WaitForRecordTest(unittest.TestCase):
    def setUp(self):
        reset_reliability_for_tests()

    def tearDown(self):
        reset_reliability_for_tests()

    def test_waiter_receives_owner_result(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, _ = claim_ai_request("k1", fp)
        result = {"ok": True, "translation": "渴望"}
        finish_ai_request(key, record, result=result)
        got, error = wait_for_ai_request_record(
            key, record, deadline_at=time.monotonic() + 1
        )
        self.assertEqual(got, result)
        self.assertIsNone(error)

    def test_waiter_receives_owner_error(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, _ = claim_ai_request("k1", fp)
        error = {"code": "AI_LLM_FAILED", "message": user_message_for("AI_LLM_FAILED")}
        finish_ai_request(key, record, error=error)
        got, err = wait_for_ai_request_record(key, record, deadline_at=time.monotonic() + 1)
        self.assertIsNone(got)
        self.assertEqual(err, error)

    def test_wait_times_out_with_total_timeout_error(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, _ = claim_ai_request("k1", fp)
        got, error = wait_for_ai_request_record(
            key, record, deadline_at=time.monotonic() + 0.01
        )
        self.assertIsNone(got)
        self.assertEqual(error["code"], "AI_TOTAL_TIMEOUT")
        self.assertEqual(error["timeoutStage"], "idempotency_wait")

    def test_cancel_check_raises_client_cancelled(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        key, record, _ = claim_ai_request("k1", fp)
        with self.assertRaises(ClientCancelledError):
            wait_for_ai_request_record(
                key,
                record,
                deadline_at=time.monotonic() + 1,
                cancel_check=lambda: True,
            )

    def test_async_waiter_receives_owner_result(self):
        async def scenario():
            fp = build_ai_request_fingerprint({"text": "crave"})
            key, record, _ = claim_ai_request("k1", fp)
            result = {"ok": True, "translation": "娓存湜"}

            async def finish_later():
                await anyio.sleep(0.01)
                finish_ai_request(key, record, result=result)

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(finish_later)
                got, error = await wait_for_ai_request_record_async(
                    key, record, deadline_at=time.monotonic() + 1
                )
                self.assertEqual(got, result)
                self.assertIsNone(error)

        anyio.run(scenario)

    def test_generation_attempts_increment(self):
        fp = build_ai_request_fingerprint({"text": "crave"})
        _, record, _ = claim_ai_request("k1", fp)
        self.assertEqual(touch_generation_attempt(record), 1)
        self.assertEqual(touch_generation_attempt(record), 2)
        self.assertEqual(record.generation_attempts, 2)

    def test_user_message_map_covers_internal_codes(self):
        messages = {
            "AI_QUEUE_FULL": "AI 服务暂时繁忙，请稍后再试",
            "AI_TOTAL_TIMEOUT": "分析超时，请稍后重试",
            "AI_LLM_FAILED": "分析服务暂时不可用，请稍后重试",
            "AI_CANCELLED": "请求已取消",
            "AI_IDEMPOTENCY_REUSED": "请求标识冲突，请刷新后重试",
            "AI_DAILY_QUOTA": "今日调用额度已用完",
            "AI_INTERNAL_ERROR": "分析服务暂时不可用，请稍后重试",
        }
        for code, expected in messages.items():
            self.assertEqual(user_message_for(code), expected)
        # Unknown internal codes fall back to the generic message.
        self.assertEqual(user_message_for("SOMETHING_NEW"), messages["AI_INTERNAL_ERROR"])
