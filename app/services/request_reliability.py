"""
AI request reliability primitives.

Owns the single per-request notion of an AI analysis operation:

* A unified monotonic deadline that covers queue wait, Qwen generation, JSON
  parsing and any internal retry.
* Client cancellation that can be observed by long-running waits.
* Idempotency-Key replay (same key + same payload -> same result, no re-run).
* In-flight deduplication (same payload already being analyzed -> join it).

The real chain is wired in ``app/main.py`` (endpoints) and
``app/services/analyzer.py`` / ``app/services/ollama_example.py``. This module
stays free of FastAPI / metrics dependencies so it can be unit-tested directly.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


AI_REQUEST_IDEMPOTENCY_TTL_SECONDS = 15 * 60

# Internal error codes are kept for logs / metrics / traces; the frontend only
# ever sees the corresponding Chinese message.
AI_ERROR_MESSAGES: dict[str, str] = {
    "AI_QUEUE_FULL": "AI 服务暂时繁忙，请稍后再试",
    "AI_TOTAL_TIMEOUT": "分析超时，请稍后重试",
    "AI_LLM_FAILED": "分析服务暂时不可用，请稍后重试",
    "AI_CANCELLED": "请求已取消",
    "AI_IDEMPOTENCY_REUSED": "请求标识冲突，请刷新后重试",
    "AI_DAILY_QUOTA": "今日调用额度已用完",
    "AI_INTERNAL_ERROR": "分析服务暂时不可用，请稍后重试",
}


class ClientCancelledError(RuntimeError):
    pass


class IdempotencyKeyReuseError(RuntimeError):
    pass


class RequestDeadlineExceededError(RuntimeError):
    pass


class StreamCancelController:
    """Coordinates client-disconnect cancellation with blocking stream readers."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._close_callbacks: list[Callable[[], None]] = []

    def cancel(self) -> None:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if not self._cancelled.is_set():
                self._cancelled.set()
                callbacks = list(self._close_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        close_now = False
        with self._lock:
            self._close_callbacks.append(callback)
            close_now = self._cancelled.is_set()
        if close_now:
            try:
                callback()
            except Exception:
                pass


def user_message_for(code: str) -> str:
    """Map an internal error code to the Chinese message shown to the user."""
    return AI_ERROR_MESSAGES.get(code, AI_ERROR_MESSAGES["AI_INTERNAL_ERROR"])


def normalize_idempotency_key(value: str | None) -> str:
    return str(value or "").strip()


def build_ai_request_fingerprint(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def remaining_seconds(deadline_at: float) -> float:
    return max(0.0, deadline_at - time.monotonic())


def total_timeout_deadline(seconds: float) -> float:
    """Start a unified deadline for the whole AI chain."""
    return time.monotonic() + max(0.0, seconds)


@dataclass
class AiRequestRecord:
    fingerprint: str
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = field(default_factory=lambda: time.monotonic() + AI_REQUEST_IDEMPOTENCY_TTL_SECONDS)
    completed_at: float | None = None
    generation_attempts: int = 0
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    event: threading.Event = field(default_factory=threading.Event)


_records_by_key: dict[str, AiRequestRecord] = {}
_inflight_by_fingerprint: dict[str, AiRequestRecord] = {}
_records_lock = threading.Lock()


def _purge_expired(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        key
        for key, record in _records_by_key.items()
        if record.expires_at <= now and record.completed_at is not None
    ]
    for key in expired:
        _records_by_key.pop(key, None)

    stale_fingerprints = [
        fingerprint
        for fingerprint, record in _inflight_by_fingerprint.items()
        if record.completed_at is not None and record.expires_at <= now
    ]
    for fingerprint in stale_fingerprints:
        _inflight_by_fingerprint.pop(fingerprint, None)


def claim_ai_request(idempotency_key: str | None, fingerprint: str) -> tuple[str | None, AiRequestRecord | None, bool]:
    """Claim the AI request identified by *idempotency_key* + *fingerprint*.

    Returns ``(key, record, is_owner)``:

    * ``is_owner=True``: this caller must run the analysis and then call
      ``finish_ai_request``.
    * ``is_owner=False``: this caller must wait for the owner via
      ``wait_for_ai_request_record`` and reuse its result.
    """
    key = normalize_idempotency_key(idempotency_key)
    with _records_lock:
        _purge_expired()

        if key:
            record = _records_by_key.get(key)
            if record is None:
                # Same payload may already be in flight under a different key
                # (or key-less): join it instead of running a second generation.
                inflight = _inflight_by_fingerprint.get(fingerprint)
                if inflight is not None:
                    _records_by_key[key] = inflight
                    return key, inflight, False
                record = AiRequestRecord(fingerprint=fingerprint)
                _records_by_key[key] = record
                _inflight_by_fingerprint[fingerprint] = record
                return key, record, True

            if record.fingerprint != fingerprint:
                raise IdempotencyKeyReuseError("Idempotency-Key was reused for a different AI request")

            return key, record, False

        record = _inflight_by_fingerprint.get(fingerprint)
        if record is not None:
            return None, record, False

        record = AiRequestRecord(fingerprint=fingerprint)
        _inflight_by_fingerprint[fingerprint] = record
        return None, record, True


def finish_ai_request(
    key: str | None,
    record: AiRequestRecord | None,
    *,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Mark a claimed AI request as finished so waiters can proceed."""
    if record is None:
        return
    with _records_lock:
        record.result = dict(result) if result is not None else None
        record.error = dict(error) if error is not None else None
        record.completed_at = time.monotonic()
        record.expires_at = record.completed_at + AI_REQUEST_IDEMPOTENCY_TTL_SECONDS
        record.event.set()
        if _inflight_by_fingerprint.get(record.fingerprint) is record:
            _inflight_by_fingerprint.pop(record.fingerprint, None)


def wait_for_ai_request_record(
    key: str | None,
    record: AiRequestRecord | None,
    *,
    deadline_at: float,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Wait for the owner of *record* to finish.

    Returns ``(result, None)`` on success or ``(None, error)`` when the owner
    finished with an error or the deadline elapsed before the owner finished.
    ``cancel_check`` returning True raises ``ClientCancelledError``.
    """
    if record is None:
        return None, None

    while True:
        if cancel_check and cancel_check():
            raise ClientCancelledError()

        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return None, {
                "code": "AI_TOTAL_TIMEOUT",
                "message": user_message_for("AI_TOTAL_TIMEOUT"),
                "timeoutStage": "idempotency_wait",
            }

        if record.event.wait(timeout=min(0.25, remaining)):
            return record.result, record.error


def touch_generation_attempt(record: AiRequestRecord | None) -> int:
    """Increment the actual-Qwen-attempt counter for a claimed request."""
    if record is None:
        return 0
    with _records_lock:
        record.generation_attempts += 1
        return record.generation_attempts


def reset_reliability_for_tests() -> None:
    """Clear in-process reliability state (tests only)."""
    with _records_lock:
        _records_by_key.clear()
        _inflight_by_fingerprint.clear()
