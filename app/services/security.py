from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date
from typing import Iterator
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resource_usage import ResourceUsage


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


def consume_daily_quota(
    db: Session,
    *,
    user_id: UUID,
    resource: str,
    limit: int,
) -> None:
    today = date.today()
    usage = db.scalar(
        select(ResourceUsage).where(
            ResourceUsage.user_id == user_id,
            ResourceUsage.resource == resource,
            ResourceUsage.usage_date == today,
        )
    )
    if usage is None:
        usage = ResourceUsage(
            user_id=user_id,
            resource=resource,
            usage_date=today,
            count=0,
        )
        db.add(usage)
        db.flush()
    if usage.count >= max(1, limit):
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
    acquired = semaphore.acquire(timeout=max(0, wait_seconds))
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务繁忙，请稍后再试",
        )
    try:
        yield
    finally:
        semaphore.release()
