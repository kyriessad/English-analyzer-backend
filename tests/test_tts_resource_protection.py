import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import anyio
from fastapi import HTTPException

from app.core.config import settings
from app.observability.metrics import (
    TTS_ACTIVE,
    TTS_QUEUE_FULL_REJECT_TOTAL,
    TTS_SLOT_TIMEOUT_TOTAL,
    TTS_WAITING,
)
from app.routers import language
from app.services import security
from app.services.security import async_resource_slot


def _metric_value(metric) -> float:
    return metric._value.get()


class TtsResourceProtectionTest(unittest.TestCase):
    def setUp(self):
        self.original_semaphore = security._resource_semaphores["tts"]
        self.original_waiting_semaphore = security._resource_waiting_semaphores["tts"]
        security._resource_semaphores["tts"] = threading.BoundedSemaphore(1)
        security._resource_waiting_semaphores["tts"] = threading.BoundedSemaphore(2)
        self.active_before = _metric_value(TTS_ACTIVE)
        self.waiting_before = _metric_value(TTS_WAITING)
        self.timeout_before = _metric_value(TTS_SLOT_TIMEOUT_TOTAL)
        self.queue_full_before = _metric_value(TTS_QUEUE_FULL_REJECT_TOTAL)

    def tearDown(self):
        security._resource_semaphores["tts"] = self.original_semaphore
        security._resource_waiting_semaphores["tts"] = self.original_waiting_semaphore
        self.assertEqual(_metric_value(TTS_ACTIVE), self.active_before)
        self.assertEqual(_metric_value(TTS_WAITING), self.waiting_before)

    async def _wait_for_waiting(self, expected: int, timeout: float = 1.0) -> None:
        target = self.waiting_before + expected
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _metric_value(TTS_WAITING) == target:
                return
            await anyio.sleep(0.005)
        self.assertEqual(_metric_value(TTS_WAITING), target)

    def test_config_defaults_are_one_active_and_two_waiting(self):
        self.assertEqual(settings.tts_global_concurrency, 1)
        self.assertEqual(settings.tts_queue_waiting_capacity, 2)

    def test_one_active_two_waiting_and_extra_requests_fail_fast(self):
        async def scenario():
            owner = async_resource_slot("tts", timeout=0.1)
            await owner.__aenter__()
            outcomes: list[str] = []
            active_peak = _metric_value(TTS_ACTIVE) - self.active_before
            waiting_peak = 0.0
            limiter = anyio.to_thread.current_default_thread_limiter()
            borrowed_before = limiter.borrowed_tokens

            async def waiter():
                async with async_resource_slot("tts", timeout=1.0):
                    outcomes.append("acquired")
                    await anyio.sleep(0.03)

            try:
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(waiter)
                    task_group.start_soon(waiter)
                    await self._wait_for_waiting(2)
                    waiting_peak = max(
                        waiting_peak,
                        _metric_value(TTS_WAITING) - self.waiting_before,
                    )

                    self.assertEqual(limiter.borrowed_tokens, borrowed_before)
                    for _ in range(2):
                        started = time.perf_counter()
                        with self.assertRaises(HTTPException) as raised:
                            async with async_resource_slot("tts", timeout=1.0):
                                pass
                        self.assertEqual(raised.exception.status_code, 503)
                        self.assertIn("queue is full", raised.exception.detail)
                        self.assertLess(time.perf_counter() - started, 0.25)

                    await owner.__aexit__(None, None, None)
                    owner = None

                active_peak = max(
                    active_peak,
                    _metric_value(TTS_ACTIVE) - self.active_before,
                )
            finally:
                if owner is not None:
                    await owner.__aexit__(None, None, None)

            self.assertEqual(outcomes, ["acquired", "acquired"])
            self.assertLessEqual(active_peak, 1)
            self.assertLessEqual(waiting_peak, 2)

        anyio.run(scenario)
        self.assertEqual(
            _metric_value(TTS_QUEUE_FULL_REJECT_TOTAL),
            self.queue_full_before + 2,
        )

    def test_wait_timeout_returns_503_and_restores_capacity(self):
        async def scenario():
            owner = async_resource_slot("tts", timeout=0.1)
            await owner.__aenter__()
            try:
                with self.assertRaises(HTTPException) as raised:
                    async with async_resource_slot("tts", timeout=0.03):
                        pass
                self.assertEqual(raised.exception.status_code, 503)
                self.assertIn("timed out", raised.exception.detail)
                self.assertEqual(_metric_value(TTS_WAITING), self.waiting_before)
            finally:
                await owner.__aexit__(None, None, None)

            async with async_resource_slot("tts", timeout=0.1):
                self.assertEqual(_metric_value(TTS_ACTIVE), self.active_before + 1)

        anyio.run(scenario)
        self.assertEqual(
            _metric_value(TTS_SLOT_TIMEOUT_TOTAL),
            self.timeout_before + 1,
        )

    def test_cancelled_waiter_and_failing_owner_release_all_permits(self):
        async def scenario():
            owner = async_resource_slot("tts", timeout=0.1)
            await owner.__aenter__()

            async def waiter():
                async with async_resource_slot("tts", timeout=1.0):
                    pass

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(waiter)
                await self._wait_for_waiting(1)
                task_group.cancel_scope.cancel()

            self.assertEqual(_metric_value(TTS_WAITING), self.waiting_before)
            await owner.__aexit__(RuntimeError, RuntimeError("boom"), None)

            with self.assertRaises(RuntimeError):
                async with async_resource_slot("tts", timeout=0.1):
                    raise RuntimeError("boom")

            async with async_resource_slot("tts", timeout=0.1):
                pass

        anyio.run(scenario)

    def test_cache_hit_bypasses_tts_queue(self):
        async def scenario(wav_path: Path):
            request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
            user = SimpleNamespace(id=uuid4())
            with (
                patch.object(language, "enforce_resource_rate_limit"),
                patch.object(language, "consume_daily_quota"),
                patch.object(language, "get_cached_audio", return_value=wav_path),
                patch.object(language, "async_resource_slot") as slot,
                patch.object(language, "synthesize_or_get_cached_audio") as synthesize,
            ):
                response = await language.get_pronunciation_audio(
                    request=request,
                    text="cached sentence",
                    voice="male",
                    current_user=user,
                    db=object(),
                )
                self.assertEqual(Path(response.path), wav_path)
                slot.assert_not_called()
                synthesize.assert_not_called()

        with TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "cached.wav"
            wav_path.write_bytes(b"0" * 45)
            anyio.run(scenario, wav_path)

    def test_cache_miss_uses_slot_and_runs_synthesis_in_worker(self):
        events: list[str] = []
        worker_threads: list[int] = []

        @asynccontextmanager
        async def tracked_slot(_resource):
            events.append("slot_enter")
            try:
                yield
            finally:
                events.append("slot_exit")

        async def scenario(wav_path: Path):
            request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
            user = SimpleNamespace(id=uuid4())

            def synthesize(*_args, **_kwargs):
                events.append("synthesize")
                worker_threads.append(threading.get_ident())
                return wav_path

            with (
                patch.object(language, "enforce_resource_rate_limit"),
                patch.object(language, "consume_daily_quota"),
                patch.object(language, "get_cached_audio", return_value=None),
                patch.object(language, "async_resource_slot", tracked_slot),
                patch.object(language, "synthesize_or_get_cached_audio", synthesize),
            ):
                event_loop_thread = threading.get_ident()
                response = await language.get_pronunciation_audio(
                    request=request,
                    text="new sentence",
                    voice="female",
                    current_user=user,
                    db=object(),
                )
                self.assertEqual(Path(response.path), wav_path)
                self.assertNotEqual(worker_threads, [event_loop_thread])

        with TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "generated.wav"
            wav_path.write_bytes(b"0" * 45)
            anyio.run(scenario, wav_path)

        self.assertEqual(events, ["slot_enter", "synthesize", "slot_exit"])


if __name__ == "__main__":
    unittest.main()
