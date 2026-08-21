"""Measure how long the bridge worker takes to exit on the SUCCESS path.

Mirrors app.main._bridge_sync_generator exactly, with timing instrumentation
around the worker's final steps and the consumer's join(), to determine why
"worker still alive after cancel()" fires on normal, fully-successful streams.
"""
import asyncio
import contextvars
import queue
import threading
import time

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.request_reliability import StreamCancelController  # noqa: E402
from app.observability.operations import record_operation_result  # noqa: E402


def sync_gen():
    """Mimic analyze_text_streaming: field events, then final, then a finally."""
    import time as _t
    for i in range(3):
        _t.sleep(0.01)
        yield ("field", f"f{i}", "x")
    yield ("final", {"ok": True})
    t_before_op = time.perf_counter()
    record_operation_result("ai", "analyze_text_streaming_result", "success", 1.0)
    print(f"  [worker] record_operation_result took {time.perf_counter()-t_before_op:.4f}s", flush=True)


def make_bridge(sync_gen, worker_ctx, *, cancel_controller):
    _sentinel = object()
    events: queue.SimpleQueue = queue.SimpleQueue()
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal():
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    def _run():
        t0 = time.perf_counter()
        try:
            while True:
                try:
                    item = worker_ctx.run(next, sync_gen)
                except StopIteration:
                    events.put(_sentinel)
                    _signal()
                    print(f"  [worker] sentinel put at +{time.perf_counter()-t0:.4f}s, exiting", flush=True)
                    return
                events.put(item)
                _signal()
        except BaseException as exc:
            events.put(exc)
            _signal()

    worker = threading.Thread(target=_run, name="bridge-timing-worker", daemon=True)
    worker.start()

    async def bridge():
        try:
            while True:
                while not events.empty():
                    item = events.get_nowait()
                    if item is _sentinel:
                        return
                    if isinstance(item, BaseException):
                        raise item
                    yield item
                wake.clear()
                if not events.empty():
                    continue
                await wake.wait()
        finally:
            t_join_start = time.perf_counter()
            cancel_controller.cancel()
            alive = worker.is_alive()
            joined = worker.join(timeout=0.05)
            print(
                f"  [bridge] finally: cancel+join at +{t_join_start-0:.4f}s (rel consumer start) "
                f"alive_before={alive} joined={joined} "
                f"join_took={time.perf_counter()-t_join_start:.4f}s "
                f"worker_still_alive={worker.is_alive()}",
                flush=True,
            )

    return bridge, worker


async def main():
    cancel = StreamCancelController()
    worker_ctx = contextvars.copy_context()
    bridge, worker = make_bridge(sync_gen(), worker_ctx, cancel_controller=cancel)
    t0 = time.perf_counter()
    n = 0
    async for ev in bridge():
        n += 1
        if ev[0] == "final":
            print(f"  [consumer] got final at +{time.perf_counter()-t0:.4f}s, breaking", flush=True)
            break
    await asyncio.sleep(0.05)
    print(f"  [consumer] after break, worker.is_alive()={worker.is_alive()} at +{time.perf_counter()-t0:.4f}s", flush=True)
    await asyncio.sleep(0.2)
    print(f"  [consumer] later, worker.is_alive()={worker.is_alive()} at +{time.perf_counter()-t0:.4f}s", flush=True)


asyncio.run(main())
