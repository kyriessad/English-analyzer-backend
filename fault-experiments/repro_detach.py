"""Faithful reproduction of the cross-context OTel detach failure.

Mimics Starlette 0.38.6 ``StreamingResponse.__call__``: a task group runs
``stream_response`` (which iterates an async generator whose ``observed_operation``
span spans the yields) alongside ``listen_for_disconnect``. When the client
disconnects mid-stream, the task group is cancelled; asyncio finalizes the
async generator by calling ``aclose()`` in a *copied* context, so OTel's
``ContextVar.reset(token)`` raises "Token was created in a different Context".

Before the fix this prints the ValueError; after the fix it must not.
"""
import asyncio
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TRACE_LOG_PATH", os.path.join(tempfile.gettempdir(), "repro_detach_traces.jsonl"))
os.environ.setdefault("TRACING_ENABLED", "true")

import anyio  # noqa: E402

from app.observability.operations import observed_operation  # noqa: E402
from app.observability.tracing import configure_tracing  # noqa: E402

configure_tracing()

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


async def ndjson_stream():
    with observed_operation(
        "ai",
        "analyze_english_stream",
        attributes={"card_type": "word", "target_lang": "zh"},
    ):
        yield b'{"type":"start"}\n'
        for i in range(4):
            await asyncio.sleep(0.02)  # let the disconnect land mid-stream
            yield json.dumps({"type": "field", "field": f"f{i}", "value": str(i)}).encode() + b"\n"
        yield b'{"type":"final","data":{"ok":true}}\n'
    yield b'{"type":"done"}\n'


failures: list[str] = []


def _record_detach_error(record: logging.LogRecord) -> None:
    if record.name == "opentelemetry.context" and "Failed to detach" in record.getMessage():
        failures.append(record.getMessage())


async def main() -> None:
    handler = logging.Handler()
    handler.emit = _record_detach_error
    logging.getLogger().addHandler(handler)

    sent = []

    async def send(msg):
        await asyncio.sleep(0.005)  # client backpressure so the stream stays mid-flight
        sent.append(msg.get("type"))

    recv_queue = asyncio.Queue()

    async def receive():
        return await recv_queue.get()

    async def stream_response():
        await send({"type": "http.response.start", "status": 200, "headers": []})
        async for chunk in ndjson_stream():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def listen_for_disconnect():
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break

    async with anyio.create_task_group() as tg:

        async def wrap(fn):
            await fn()
            tg.cancel_scope.cancel()

        tg.start_soon(wrap, stream_response)
        await asyncio.sleep(0.06)  # client disconnects mid-stream
        await recv_queue.put({"type": "http.disconnect"})
        await wrap(listen_for_disconnect)

    # Let asyncio's asyncgen finalizer run aclose() in a copied context.
    await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)

    if failures:
        print("REPRO RESULT: FAIL  ->", failures[:3])
        raise SystemExit(1)
    print("REPRO RESULT: PASS  (no cross-context detach error)")
    print("sent types:", [t for t in sent])


asyncio.run(main())
