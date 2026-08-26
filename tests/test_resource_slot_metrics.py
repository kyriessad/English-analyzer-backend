import threading
import time
import unittest

import anyio
from fastapi import HTTPException

from app.observability.metrics import (
    AI_ACTIVE,
    AI_QUEUE_FULL_REJECT_TOTAL,
    AI_SLOT_TIMEOUT_TOTAL,
    AI_SLOT_WAIT_SECONDS,
    AI_WAITING,
)
from app.services import security
from app.services.security import async_resource_slot, resource_slot


def _metric_value(metric) -> float:
    return metric._value.get()


def _histogram_count(metric) -> float:
    for collected in metric.collect():
        for sample in collected.samples:
            if sample.name == f"{metric._name}_count":
                return sample.value
    raise AssertionError(f"{metric._name}_count sample not found")


class ResourceSlotMetricsTest(unittest.TestCase):
    def setUp(self):
        self.original_ai_semaphore = security._resource_semaphores["ai"]
        self.original_waiting_semaphore = security._resource_waiting_semaphores["ai"]
        security._resource_semaphores["ai"] = threading.BoundedSemaphore(1)
        security._resource_waiting_semaphores["ai"] = threading.BoundedSemaphore(2)
        self.active_before = _metric_value(AI_ACTIVE)
        self.waiting_before = _metric_value(AI_WAITING)
        self.timeout_before = _metric_value(AI_SLOT_TIMEOUT_TOTAL)
        self.queue_full_before = _metric_value(AI_QUEUE_FULL_REJECT_TOTAL)
        self.wait_count_before = _histogram_count(AI_SLOT_WAIT_SECONDS)

    def tearDown(self):
        security._resource_semaphores["ai"] = self.original_ai_semaphore
        security._resource_waiting_semaphores["ai"] = self.original_waiting_semaphore
        self.assertEqual(_metric_value(AI_WAITING), self.waiting_before)
        self.assertEqual(_metric_value(AI_ACTIVE), self.active_before)

    def _wait_for_waiting(self, expected: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        target = self.waiting_before + expected
        while time.monotonic() < deadline:
            if _metric_value(AI_WAITING) == target:
                return
            time.sleep(0.005)
        self.assertEqual(_metric_value(AI_WAITING), target)

    def test_direct_acquire_does_not_consume_waiting_capacity(self):
        waiting_semaphore = security._resource_waiting_semaphores["ai"]
        with resource_slot("ai", timeout=0.1):
            self.assertEqual(_metric_value(AI_WAITING), self.waiting_before)
            self.assertEqual(_metric_value(AI_ACTIVE), self.active_before + 1)
            self.assertTrue(waiting_semaphore.acquire(blocking=False))
            self.assertTrue(waiting_semaphore.acquire(blocking=False))
            self.assertFalse(waiting_semaphore.acquire(blocking=False))
            waiting_semaphore.release()
            waiting_semaphore.release()

        self.assertEqual(_metric_value(AI_ACTIVE), self.active_before)
        self.assertEqual(_histogram_count(AI_SLOT_WAIT_SECONDS), self.wait_count_before + 1)

    def test_two_waiters_are_admitted(self):
        owner = resource_slot("ai", timeout=0.1)
        owner.__enter__()
        release_waiters = threading.Event()
        entered = threading.Semaphore(0)

        def wait_for_slot():
            with resource_slot("ai", timeout=1.0):
                entered.release()
                release_waiters.wait(1.0)

        waiters = [threading.Thread(target=wait_for_slot) for _ in range(2)]
        for waiter in waiters:
            waiter.start()
        try:
            self._wait_for_waiting(2)
            self.assertEqual(_metric_value(AI_ACTIVE), self.active_before + 1)
        finally:
            owner.__exit__(None, None, None)
            release_waiters.set()
            for waiter in waiters:
                waiter.join(timeout=2)

        self.assertTrue(all(not waiter.is_alive() for waiter in waiters))
        self.assertTrue(entered.acquire(timeout=0.1))
        self.assertTrue(entered.acquire(timeout=0.1))

    def test_third_waiter_is_rejected_immediately(self):
        owner = resource_slot("ai", timeout=0.1)
        owner.__enter__()
        release_waiters = threading.Event()

        def wait_for_slot():
            with resource_slot("ai", timeout=1.0):
                release_waiters.wait(1.0)

        waiters = [threading.Thread(target=wait_for_slot) for _ in range(2)]
        for waiter in waiters:
            waiter.start()
        try:
            self._wait_for_waiting(2)
            started = time.perf_counter()
            with self.assertRaises(HTTPException) as raised:
                with resource_slot("ai", timeout=1.0):
                    pass
            elapsed = time.perf_counter() - started

            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(raised.exception.detail, "AI 当前繁忙，请稍后重试")
            self.assertLess(elapsed, 0.25)
            self.assertEqual(_metric_value(AI_WAITING), self.waiting_before + 2)
            self.assertEqual(
                _metric_value(AI_QUEUE_FULL_REJECT_TOTAL),
                self.queue_full_before + 1,
            )
            self.assertEqual(_metric_value(AI_SLOT_TIMEOUT_TOTAL), self.timeout_before)
        finally:
            owner.__exit__(None, None, None)
            release_waiters.set()
            for waiter in waiters:
                waiter.join(timeout=2)

    def test_promoted_waiter_releases_capacity_for_a_new_waiter(self):
        owner = resource_slot("ai", timeout=0.1)
        owner.__enter__()
        promoted = threading.Event()
        release_promoted = threading.Event()
        replacement_entered = threading.Event()

        def first_waiter():
            with resource_slot("ai", timeout=1.0):
                promoted.set()
                release_promoted.wait(1.0)

        def replacement_waiter():
            with resource_slot("ai", timeout=1.0):
                replacement_entered.set()

        first = threading.Thread(target=first_waiter)
        first.start()
        self._wait_for_waiting(1)
        owner.__exit__(None, None, None)
        self.assertTrue(promoted.wait(1.0))
        self._wait_for_waiting(0)

        replacement = threading.Thread(target=replacement_waiter)
        replacement.start()
        try:
            self._wait_for_waiting(1)
        finally:
            release_promoted.set()
            first.join(timeout=2)
            replacement.join(timeout=2)

        self.assertTrue(replacement_entered.is_set())

    def test_wait_timeout_releases_waiting_capacity(self):
        owner = resource_slot("ai", timeout=0.1)
        owner.__enter__()
        try:
            with self.assertRaises(HTTPException) as raised:
                with resource_slot("ai", timeout=0.02):
                    pass
            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(_metric_value(AI_SLOT_TIMEOUT_TOTAL), self.timeout_before + 1)
            self.assertEqual(_metric_value(AI_WAITING), self.waiting_before)

            waiting_semaphore = security._resource_waiting_semaphores["ai"]
            self.assertTrue(waiting_semaphore.acquire(blocking=False))
            self.assertTrue(waiting_semaphore.acquire(blocking=False))
            waiting_semaphore.release()
            waiting_semaphore.release()
        finally:
            owner.__exit__(None, None, None)

    def test_wait_cancel_releases_waiting_capacity(self):
        class TestCancellation(BaseException):
            pass

        class CancellingSemaphore:
            def acquire(self, blocking=True, timeout=None):
                if not blocking:
                    return False
                raise TestCancellation()

        security._resource_semaphores["ai"] = CancellingSemaphore()
        with self.assertRaises(TestCancellation):
            with resource_slot("ai", timeout=1.0):
                pass

        self.assertEqual(_metric_value(AI_WAITING), self.waiting_before)
        waiting_semaphore = security._resource_waiting_semaphores["ai"]
        self.assertTrue(waiting_semaphore.acquire(blocking=False))
        self.assertTrue(waiting_semaphore.acquire(blocking=False))
        self.assertFalse(waiting_semaphore.acquire(blocking=False))
        waiting_semaphore.release()
        waiting_semaphore.release()

    def test_active_slot_releases_when_body_raises(self):
        with self.assertRaises(RuntimeError):
            with resource_slot("ai", timeout=0.1):
                self.assertEqual(_metric_value(AI_ACTIVE), self.active_before + 1)
                raise RuntimeError("boom")

        self.assertEqual(_metric_value(AI_ACTIVE), self.active_before)

    def test_concurrent_arrivals_never_exceed_waiting_capacity(self):
        owner = resource_slot("ai", timeout=0.1)
        owner.__enter__()
        start = threading.Barrier(7)
        release_successes = threading.Event()
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def contender():
            start.wait()
            try:
                with resource_slot("ai", timeout=1.0):
                    with outcomes_lock:
                        outcomes.append("acquired")
                    release_successes.wait(1.0)
            except HTTPException as exc:
                with outcomes_lock:
                    outcomes.append(f"rejected:{exc.status_code}")

        contenders = [threading.Thread(target=contender) for _ in range(6)]
        for contender_thread in contenders:
            contender_thread.start()
        start.wait()

        max_waiting = 0.0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            current_waiting = _metric_value(AI_WAITING) - self.waiting_before
            max_waiting = max(max_waiting, current_waiting)
            with outcomes_lock:
                rejected = sum(item.startswith("rejected:") for item in outcomes)
            if current_waiting == 2 and rejected == 4:
                break
            time.sleep(0.002)

        try:
            self.assertEqual(_metric_value(AI_WAITING), self.waiting_before + 2)
            self.assertLessEqual(max_waiting, 2)
            self.assertEqual(
                _metric_value(AI_QUEUE_FULL_REJECT_TOTAL),
                self.queue_full_before + 4,
            )
        finally:
            owner.__exit__(None, None, None)
            release_successes.set()
            for contender_thread in contenders:
                contender_thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in contenders))
        self.assertEqual(outcomes.count("acquired"), 2)
        self.assertEqual(outcomes.count("rejected:503"), 4)

    def test_async_slot_waiters_are_limited_and_third_is_rejected(self):
        async def wait_for_waiting(expected: int, timeout: float = 1.0) -> None:
            deadline = time.monotonic() + timeout
            target = self.waiting_before + expected
            while time.monotonic() < deadline:
                if _metric_value(AI_WAITING) == target:
                    return
                await anyio.sleep(0.005)
            self.assertEqual(_metric_value(AI_WAITING), target)

        async def scenario():
            owner = async_resource_slot("ai", timeout=0.1)
            await owner.__aenter__()

            async def waiter():
                async with async_resource_slot("ai", timeout=1.0):
                    await anyio.sleep(1.0)

            try:
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(waiter)
                    task_group.start_soon(waiter)
                    await wait_for_waiting(2)

                    started = time.perf_counter()
                    with self.assertRaises(HTTPException) as raised:
                        async with async_resource_slot("ai", timeout=1.0):
                            pass
                    elapsed = time.perf_counter() - started

                    self.assertEqual(raised.exception.status_code, 503)
                    self.assertEqual(raised.exception.detail, "AI 当前繁忙，请稍后重试")
                    self.assertLess(elapsed, 0.25)
                    self.assertEqual(_metric_value(AI_WAITING), self.waiting_before + 2)
                    task_group.cancel_scope.cancel()
            finally:
                await owner.__aexit__(None, None, None)

        anyio.run(scenario)

    def test_async_wait_timeout_has_readable_text_and_releases_capacity(self):
        async def scenario():
            owner = async_resource_slot("ai", timeout=0.1)
            await owner.__aenter__()
            try:
                with self.assertRaises(HTTPException) as raised:
                    async with async_resource_slot("ai", timeout=0.02):
                        pass

                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.detail, "服务繁忙，请稍后再试")
                self.assertEqual(_metric_value(AI_SLOT_TIMEOUT_TOTAL), self.timeout_before + 1)
                self.assertEqual(_metric_value(AI_WAITING), self.waiting_before)
                self.assertEqual(_metric_value(AI_ACTIVE), self.active_before + 1)
            finally:
                await owner.__aexit__(None, None, None)

        anyio.run(scenario)


if __name__ == "__main__":
    unittest.main()
