from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from typing import AsyncIterator, Iterator
from uuid import UUID

import anyio
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resource_usage import ResourceUsage
from app.observability.metrics import (
    AI_ACTIVE,
    AI_QUEUE_FULL_REJECT_TOTAL,
    AI_SLOT_TIMEOUT_TOTAL,
    AI_SLOT_WAIT_SECONDS,
    AI_WAITING,
    TTS_ACTIVE,
    TTS_QUEUE_FULL_REJECT_TOTAL,
    TTS_SLOT_TIMEOUT_TOTAL,
    TTS_SLOT_WAIT_SECONDS,
    TTS_WAITING,
)


def client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


class WindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= max(1, limit):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="请求过于频繁，请稍后再试",
                    headers={"Retry-After": str(window_seconds)},
                )
            events.append(now)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


rate_limiter = WindowRateLimiter()


_resource_semaphores = {
    "ai": threading.BoundedSemaphore(max(1, settings.ai_global_concurrency)),
    "tts": threading.BoundedSemaphore(max(1, settings.tts_global_concurrency)),
}
_resource_waiting_semaphores = {
    "ai": threading.BoundedSemaphore(max(0, settings.ai_queue_waiting_capacity)),
    "tts": threading.BoundedSemaphore(max(0, settings.tts_queue_waiting_capacity)),
}


def check_daily_quota(
    db: Session,
    *,
    user_id: UUID,
    resource: str,
    limit: int,
) -> None:
    """Check quota before queueing without reserving a unit."""
    usage = db.scalar(
        select(ResourceUsage).where(
            ResourceUsage.user_id == user_id,
            ResourceUsage.resource == resource,
            ResourceUsage.usage_date == date.today(),
        )
    )
    over_limit = usage is not None and usage.count >= max(1, limit)
    db.rollback()
    if over_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日调用额度已用完",
        )


def consume_daily_quota(
    db: Session,
    *,
    user_id: UUID,
    resource: str,
    limit: int,
) -> None:
    """Atomically reserve one unit and commit a short transaction."""
    today = date.today()
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            pg_insert(ResourceUsage)
            .values(user_id=user_id, resource=resource, usage_date=today, count=0)
            .on_conflict_do_nothing(
                index_elements=[
                    ResourceUsage.user_id,
                    ResourceUsage.resource,
                    ResourceUsage.usage_date,
                ]
            )
        )
    else:
        usage = db.scalar(
            select(ResourceUsage).where(
                ResourceUsage.user_id == user_id,
                ResourceUsage.resource == resource,
                ResourceUsage.usage_date == today,
            )
        )
        if usage is None:
            db.add(ResourceUsage(user_id=user_id, resource=resource, usage_date=today, count=0))
            db.flush()

    usage = db.scalar(
        select(ResourceUsage)
        .where(
            ResourceUsage.user_id == user_id,
            ResourceUsage.resource == resource,
            ResourceUsage.usage_date == today,
        )
        .with_for_update()
    )
    if usage is None or usage.count >= max(1, limit):
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日调用额度已用完",
        )
    usage.count += 1
    db.commit()


def enforce_resource_rate_limit(request: Request, user_id: UUID, resource: str) -> None:
    rate_limiter.check(
        f"resource:user:{resource}:{user_id}",
        limit=30 if resource == "ai" else 120,
        window_seconds=60,
    )
    rate_limiter.check(
        f"resource:ip:{resource}:{client_ip(request)}",
        limit=60 if resource == "ai" else 240,
        window_seconds=60,
    )


@contextmanager
def resource_slot(resource: str, timeout: float | None = None) -> Iterator[None]:
    semaphore = _resource_semaphores.get(resource)
    if semaphore is None:
        yield
        return
    wait_seconds = settings.resource_queue_timeout_seconds if timeout is None else timeout
    record_ai_metrics = resource == "ai"
    wait_started = time.perf_counter()
    acquired = semaphore.acquire(blocking=False)

    if record_ai_metrics and not acquired:
        waiting_semaphore = _resource_waiting_semaphores["ai"]
        waiting_permit_acquired = waiting_semaphore.acquire(blocking=False)
        if not waiting_permit_acquired:
            AI_QUEUE_FULL_REJECT_TOTAL.inc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI 当前繁忙，请稍后重试",
            )

        waiting_metric_incremented = False
        try:
            AI_WAITING.inc()
            waiting_metric_incremented = True
            acquired = semaphore.acquire(timeout=max(0, wait_seconds))
        finally:
            if waiting_metric_incremented:
                AI_WAITING.dec()
            waiting_semaphore.release()
            AI_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)
    elif record_ai_metrics:
        AI_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)
    elif not acquired:
        acquired = semaphore.acquire(timeout=max(0, wait_seconds))

    if not acquired:
        if record_ai_metrics:
            AI_SLOT_TIMEOUT_TOTAL.inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务繁忙，请稍后再试",
        )
    if record_ai_metrics:
        AI_ACTIVE.inc()
    try:
        yield
    finally:
        if record_ai_metrics:
            AI_ACTIVE.dec()
        semaphore.release()


@asynccontextmanager
async def _async_tts_resource_slot(timeout: float | None = None) -> AsyncIterator[None]:
    semaphore = _resource_semaphores["tts"]
    wait_seconds = settings.resource_queue_timeout_seconds if timeout is None else timeout
    wait_started = time.perf_counter()
    acquired = semaphore.acquire(blocking=False)

    if not acquired:
        waiting_semaphore = _resource_waiting_semaphores["tts"]
        if not waiting_semaphore.acquire(blocking=False):
            TTS_QUEUE_FULL_REJECT_TOTAL.inc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TTS queue is full. Please try again later.",
            )

        waiting_metric_incremented = False
        try:
            TTS_WAITING.inc()
            waiting_metric_incremented = True
            deadline = time.monotonic() + max(0, wait_seconds)
            while True:
                acquired = semaphore.acquire(blocking=False)
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await anyio.sleep(min(0.05, remaining))
        finally:
            if waiting_metric_incremented:
                TTS_WAITING.dec()
            waiting_semaphore.release()
            TTS_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)
    else:
        TTS_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)

    if not acquired:
        TTS_SLOT_TIMEOUT_TOTAL.inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS queue wait timed out. Please try again later.",
        )

    TTS_ACTIVE.inc()
    try:
        yield
    finally:
        TTS_ACTIVE.dec()
        semaphore.release()


@asynccontextmanager
async def async_resource_slot(resource: str, timeout: float | None = None) -> AsyncIterator[None]:
    if resource == "tts":
        async with _async_tts_resource_slot(timeout):
            yield
        return

    semaphore = _resource_semaphores.get(resource)
    if semaphore is None:
        yield
        return
    wait_seconds = settings.resource_queue_timeout_seconds if timeout is None else timeout
    record_ai_metrics = resource == "ai"
    wait_started = time.perf_counter()
    acquired = semaphore.acquire(blocking=False)

    if record_ai_metrics and not acquired:
        waiting_semaphore = _resource_waiting_semaphores["ai"]
        waiting_permit_acquired = waiting_semaphore.acquire(blocking=False)
        if not waiting_permit_acquired:
            AI_QUEUE_FULL_REJECT_TOTAL.inc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI 当前繁忙，请稍后重试",
            )

        waiting_metric_incremented = False
        try:
            AI_WAITING.inc()
            waiting_metric_incremented = True
            deadline = time.monotonic() + max(0, wait_seconds)
            while True:
                acquired = semaphore.acquire(blocking=False)
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await anyio.sleep(min(0.05, remaining))
        finally:
            if waiting_metric_incremented:
                AI_WAITING.dec()
            waiting_semaphore.release()
            AI_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)
    elif record_ai_metrics:
        AI_SLOT_WAIT_SECONDS.observe(time.perf_counter() - wait_started)
    elif not acquired:
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            acquired = semaphore.acquire(blocking=False)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await anyio.sleep(min(0.05, remaining))

    if not acquired:
        if record_ai_metrics:
            AI_SLOT_TIMEOUT_TOTAL.inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务繁忙，请稍后再试",
        )
    if record_ai_metrics:
        AI_ACTIVE.inc()
    try:
        yield
    finally:
        if record_ai_metrics:
            AI_ACTIVE.dec()
        semaphore.release()
