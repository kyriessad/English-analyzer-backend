from __future__ import annotations

import asyncio
import contextvars
from functools import partial
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import anyio

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import (
    SessionLocal,
    assert_expected_database,
    get_database_runtime_info,
    get_expected_alembic_revision,
    get_db,
)
from app.models.user import User
from app.routers.auth import router as auth_router
from app.routers.cards import router as cards_router
from app.routers.language import router as language_router
from app.routers.reviews import review_sessions_router, router as reviews_router
from app.schemas import AnalyzeRequest, AnalyzeResponse
from app.observability.context import reset_request_id, set_request_id
from app.observability.errors import classify_exception, is_db_pool_timeout, result_from_status
from app.observability.logging import configure_logging, log_event
from app.observability.metrics import (
    AI_INFLIGHT_FOLLOWER_REJECT_TOTAL,
    AI_INFLIGHT_FOLLOWERS,
    AI_REQUEST_DURATION_SECONDS,
    AI_REQUEST_EVENTS_TOTAL,
    AI_REQUESTS_TOTAL,
    DB_POOL_TIMEOUT_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_PROGRESS,
    HTTP_REQUESTS_TOTAL,
    metrics_response,
)
from app.observability.operations import observed_operation
from app.observability.tracing import configure_tracing, start_span
from app.services.analyzer import analyze_text, analyze_text_streaming
from app.services.auth_service import get_current_user
from app.services.piper_service import warmup_voices
import app.services.request_reliability as request_reliability_module
from app.services.request_reliability import (
    IdempotencyKeyReuseError,
    StreamCancelController,
    build_ai_request_fingerprint,
    claim_ai_request,
    finish_ai_request,
    remaining_seconds,
    total_timeout_deadline,
    user_message_for,
    wait_for_ai_request_record_async,
)
from app.services.security import (
    async_resource_slot,
    check_daily_quota,
    consume_daily_quota,
    enforce_resource_rate_limit,
)
from app.services.runtime_diagnostics import add_request_log, utc_timestamp


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
configure_logging()
configure_tracing()
logger = logging.getLogger(__name__)
APP_MODULE_LOADED_AT = utc_timestamp()
STREAM_DIAGNOSTIC_VERSION = "ai-stream-diag-20260824-1"
_ai_inflight_follower_semaphore = threading.BoundedSemaphore(
    max(0, settings.ai_inflight_follower_capacity)
)


def _valid_request_id(value: str | None) -> bool:
    if not value:
        return False
    if len(value) > 128:
        return False
    return all(ch.isalnum() or ch in "._:-" for ch in value)


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "__unmatched__"


def _set_standard_headers(request: Request, response: Response, request_id: str) -> None:
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; media-src 'self' blob:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path == "/api" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"


def _mime_warning(path: str, status_code: int, response_mime: str) -> str:
    if status_code >= 400:
        return ""
    mime = response_mime.lower()
    if path.endswith((".js", ".mjs")) and "javascript" not in mime:
        return f"JavaScript asset returned {response_mime or 'no Content-Type'}"
    if path.endswith(".css") and not mime.startswith("text/css"):
        return f"CSS asset returned {response_mime or 'no Content-Type'}"
    if path == "/build-info.json" and "application/json" not in mime:
        return f"build-info.json returned {response_mime or 'no Content-Type'}"
    return ""


def _record_request_response(
    request: Request,
    *,
    request_id: str,
    started: float,
    status_code: int,
    response_mime: str,
    exception: str = "",
) -> None:
    record = {
        "timestamp": utc_timestamp(),
        "method": request.method,
        "path": request.url.path,
        "status": status_code,
        "userAgent": request.headers.get("user-agent", "")[:512],
        "referer": request.headers.get("referer", "").partition("?")[0][:1000],
        "requestContentType": request.headers.get("content-type", "")[:256],
        "responseMime": response_mime[:256],
        "durationMs": round((time.perf_counter() - started) * 1000, 3),
        "requestId": request_id,
        "mimeWarning": _mime_warning(request.url.path, status_code, response_mime),
    }
    if exception:
        record["exception"] = exception
    add_request_log(record)
    if (
        request.url.path == "/build-info.json"
        or request.url.path.startswith("/assets/")
        or request.url.path.startswith("/api/")
        or status_code >= 400
        or record["mimeWarning"]
    ):
        logger.info("SERVER_REQUEST %s", jsonable_encoder(record))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "Effective Configuration: DB pool %s + %s, timeout %ss; "
        "HTTP concurrency %s; AI %s (waiting %s, followers %s); "
        "TTS %s (waiting %s); JWT expiry %s days",
        settings.db_pool_size,
        settings.db_max_overflow,
        settings.db_pool_timeout,
        settings.http_limit_concurrency,
        settings.ai_global_concurrency,
        settings.ai_queue_waiting_capacity,
        settings.ai_inflight_follower_capacity,
        settings.tts_global_concurrency,
        settings.tts_queue_waiting_capacity,
        settings.jwt_expire_days,
    )
    log_event(
        logger,
        logging.INFO,
        "runtime_code_identity",
        process_id=os.getpid(),
        working_directory=os.getcwd(),
        app_main_file=os.path.abspath(__file__),
        request_reliability_file=os.path.abspath(request_reliability_module.__file__),
        module_loaded_at=APP_MODULE_LOADED_AT,
        diagnostic_version=STREAM_DIAGNOSTIC_VERSION,
    )
    expected_revision = get_expected_alembic_revision()
    database_info = get_database_runtime_info()
    logger.info("Database dialect: %s", database_info.dialect)
    logger.info("Database host: %s", database_info.host)
    logger.info("Database port: %s", database_info.port)
    logger.info("Database name: %s", database_info.database)
    logger.info("Database schema: %s", database_info.schema)
    logger.info("Database current user: %s", database_info.current_user)
    logger.info("Database URL source: %s", database_info.url_source)
    logger.info(
        "Actual Alembic revision: %s",
        ",".join(database_info.alembic_revisions) or "missing",
    )
    logger.info("Expected Alembic revision: %s", expected_revision.revision)
    logger.info("Expected revision source: %s", expected_revision.source)
    assert_expected_database(database_info, expected_revision)

    # Preload Piper voices into the in-process cache so the first real TTS
    # request does not pay the model-load latency. Failures are logged but do
    # not block startup (TTS is degraded, not fatal).
    for voice, status in warmup_voices().items():
        if status == "ok":
            logger.info("[STARTUP] Piper %s warmup OK", voice)
        else:
            logger.error("[STARTUP] Piper %s warmup FAILED: %s", voice, status)

    yield


public_docs = settings.app_env == "development"
app = FastAPI(
    title="English Analyzer Backend",
    version="1.0.0-phase-1",
    docs_url="/docs" if public_docs else None,
    redoc_url="/redoc" if public_docs else None,
    openapi_url="/openapi.json" if public_docs else None,
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(settings.allowed_hosts) or ["127.0.0.1", "localhost"],
)
app.add_middleware(GZipMiddleware, minimum_size=700)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = (
        request.headers.get("x-request-id")
        if _valid_request_id(request.headers.get("x-request-id"))
        else uuid.uuid4().hex
    )
    request.state.request_id = request_id
    request_id_token = set_request_id(request_id)

    body_limit = settings.max_request_body_bytes

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > body_limit:
                response = JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body is too large", "request_id": request_id},
                )
                _record_request_response(
                    request,
                    request_id=request_id,
                    started=started,
                    status_code=response.status_code,
                    response_mime=response.headers.get("content-type", ""),
                )
                _set_standard_headers(request, response, request_id)
                reset_request_id(request_id_token)
                return response
        except ValueError:
            response = JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid Content-Length", "request_id": request_id},
            )
            _record_request_response(
                request,
                request_id=request_id,
                started=started,
                status_code=response.status_code,
                response_mime=response.headers.get("content-type", ""),
            )
            _set_standard_headers(request, response, request_id)
            reset_request_id(request_id_token)
            return response
    elif request.method in UNSAFE_METHODS:
        body = await request.body()
        if len(body) > body_limit:
            response = JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": "Request body is too large", "request_id": request_id},
            )
            _record_request_response(
                request,
                request_id=request_id,
                started=started,
                status_code=response.status_code,
                response_mime=response.headers.get("content-type", ""),
            )
            _set_standard_headers(request, response, request_id)
            reset_request_id(request_id_token)
            return response

    route = _request_route(request)
    HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, route=route).inc()
    try:
        try:
            with start_span(
                f"http.{request.method.lower()}",
                {
                    "http.method": request.method,
                    "http.target": request.url.path,
                    "http.route": route,
                    "request_id": request_id,
                },
            ) as span:
                response = await call_next(request)
                if span is not None:
                    span.set_attribute("http.status_code", response.status_code)
                    span.set_attribute("http.route", _request_route(request))
        except BaseException as exc:
            result = "cancelled" if classify_exception(exc) == "cancelled" else "server_error"
            route = _request_route(request)
            duration = time.perf_counter() - started
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                route=route,
                status="500",
                result=result,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                route=route,
                status="500",
                result=result,
            ).observe(duration)
            log_event(
                logger,
                logging.ERROR,
                "http_request_finished",
                method=request.method,
                route=route,
                path=request.url.path,
                status_code=500,
                result=result,
                error_type=classify_exception(exc),
                duration_ms=round(duration * 1000, 3),
            )
            _record_request_response(
                request,
                request_id=request_id,
                started=started,
                status_code=500,
                response_mime="",
                exception=type(exc).__name__,
            )
            logger.exception("Unhandled request exception request_id=%s", request_id)
            raise
        finally:
            HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, route=_request_route(request)).dec()

        _set_standard_headers(request, response, request_id)
        route = _request_route(request)
        result = result_from_status(response.status_code)
        duration = time.perf_counter() - started
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            route=route,
            status=str(response.status_code),
            result=result,
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method,
            route=route,
            status=str(response.status_code),
            result=result,
        ).observe(duration)
        log_event(
            logger,
            logging.INFO if result == "success" else logging.WARNING,
            "http_request_finished",
            method=request.method,
            route=route,
            path=request.url.path,
            status_code=response.status_code,
            result=result,
            duration_ms=round(duration * 1000, 3),
        )
        _record_request_response(
            request,
            request_id=request_id,
            started=started,
            status_code=response.status_code,
            response_mime=response.headers.get("content-type", ""),
        )
        return response
    finally:
        reset_request_id(request_id_token)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "request_id": getattr(request.state, "request_id", None),
        },
        headers=exc.headers,
    )


@app.exception_handler(SQLAlchemyTimeoutError)
async def db_pool_timeout_handler(
    request: Request,
    exc: SQLAlchemyTimeoutError,
) -> JSONResponse:
    if not is_db_pool_timeout(exc):
        raise exc

    request_id = getattr(request.state, "request_id", None)
    DB_POOL_TIMEOUT_TOTAL.inc()
    logger.error(
        "Database pool checkout timed out request_id=%s error_code=DB_POOL_TIMEOUT",
        request_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "code": "DB_POOL_TIMEOUT",
            "detail": "服务器当前请求较多，请稍后重试",
            "request_id": request_id,
        },
    )


app.include_router(auth_router)
app.include_router(cards_router)
app.include_router(reviews_router)
app.include_router(review_sessions_router)
app.include_router(language_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = metrics_response()
    return Response(content=body, media_type=content_type)


def _valid_idempotency_key(value: str | None) -> bool:
    if not value:
        return False
    if len(value) > 128:
        return False
    return all(ch.isalnum() or ch in "._:-" for ch in value)


def _ai_request_fingerprint(payload: AnalyzeRequest) -> str:
    return build_ai_request_fingerprint(
        {
            "text": str(payload.text or "").strip(),
            "cardType": payload.cardType,
            "targetLang": payload.targetLang,
            "forceRefresh": bool(payload.forceRefresh),
        }
    )


def _ai_event(event: str, result: str, **extra: object) -> None:
    AI_REQUEST_EVENTS_TOTAL.labels(operation="ai", event=event, result=result).inc()
    log_event(
        logger,
        logging.INFO,
        "ai_request_event",
        operation="ai",
        ai_event=event,
        result=result,
        **extra,
    )


@contextmanager
def _ai_inflight_follower_slot() -> Iterator[None]:
    semaphore = _ai_inflight_follower_semaphore
    acquired = semaphore.acquire(blocking=False)
    if not acquired:
        AI_INFLIGHT_FOLLOWER_REJECT_TOTAL.inc()
        _ai_event("failure", "AI_INFLIGHT_FOLLOWER_FULL")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=user_message_for("AI_INFLIGHT_FOLLOWER_FULL"),
        )

    AI_INFLIGHT_FOLLOWERS.inc()
    try:
        yield
    finally:
        AI_INFLIGHT_FOLLOWERS.dec()
        semaphore.release()


async def _wait_for_ai_follower_request_async(
    key: str | None,
    record: object | None,
    *,
    deadline_at: float,
) -> tuple[dict | None, dict | None]:
    if record is not None and not record.event.is_set():
        with _ai_inflight_follower_slot():
            return await wait_for_ai_request_record_async(key, record, deadline_at=deadline_at)
    return await wait_for_ai_request_record_async(key, record, deadline_at=deadline_at)


def _log_ai_idempotency_claim(
    *,
    key: str | None,
    fingerprint: str,
    record: object | None,
    is_owner: bool,
    force_refresh: bool,
) -> None:
    log_event(
        logger,
        logging.INFO,
        "ai_idempotency_claim",
        result="owner" if is_owner else "replay",
        idempotency_key=key or "",
        fingerprint=fingerprint[:12],
        record_id=hex(id(record)) if record is not None else "",
        force_refresh=force_refresh,
    )


def _finish_stream_ai_request(
    key: str | None,
    record: object | None,
    *,
    result: dict | None = None,
    error: dict | None = None,
) -> None:
    finish_ai_request(key, record, result=result, error=error)
    log_event(
        logger,
        logging.INFO,
        "ai_idempotency_finalize",
        result="success" if result is not None else "error",
        status=(error or {}).get("code", "success"),
        idempotency_key=key or "",
        record_id=hex(id(record)) if record is not None else "",
        generation_attempts=int(getattr(record, "generation_attempts", 0) or 0),
    )


def _slot_timeout(deadline_at: float) -> float:
    """Queue-wait budget: the configured cap, but never past the unified deadline."""
    return min(settings.ai_queue_timeout_seconds, remaining_seconds(deadline_at))


def _reliability_error_response(error: dict | None, text: str) -> dict:
    code = (error or {}).get("code", "AI_INTERNAL_ERROR")
    message = (error or {}).get("message") or user_message_for(code)
    return {
        "ok": False,
        "level": "failed",
        "category": "unknown",
        "normalizedText": str(text or "").strip(),
        "warnings": [],
        "errors": [message],
        "provider": None,
    }


def _ndjson_final_stream(data: dict, *, attempt: int = 1) -> Iterator[str]:
    yield json.dumps({"type": "start", "attempt": attempt}, ensure_ascii=False) + "\n"
    yield json.dumps(
        {"type": "final", "data": data, "attempt": attempt},
        ensure_ascii=False,
    ) + "\n"
    yield json.dumps({"type": "done"}, ensure_ascii=False) + "\n"


async def _run_sync(func, *args, **kwargs):
    return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))


def _analyze_text_observed_sync(
    payload: AnalyzeRequest,
    *,
    deadline_at: float,
    record: object | None,
) -> dict:
    with observed_operation(
        "ai",
        "analyze_english",
        attributes={
            "card_type": payload.cardType,
            "target_lang": payload.targetLang,
            "force_refresh": payload.forceRefresh,
        },
    ):
        return analyze_text(
            text=payload.text,
            card_type=payload.cardType,
            target_lang=payload.targetLang,
            force_refresh=payload.forceRefresh,
            deadline_at=deadline_at,
            record=record,
        )


@app.post("/api/analyze-english", response_model=AnalyzeResponse)
async def analyze_english(
    payload: AnalyzeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalyzeResponse:
    enforce_resource_rate_limit(request, current_user.id, "ai")
    deadline_at = total_timeout_deadline(settings.ai_total_timeout_seconds)
    idempotency_key = (
        request.headers.get("idempotency-key")
        if _valid_idempotency_key(request.headers.get("idempotency-key"))
        else None
    )
    fingerprint = _ai_request_fingerprint(payload)

    try:
        key, record, is_owner = claim_ai_request(current_user.id, idempotency_key, fingerprint)
    except IdempotencyKeyReuseError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=user_message_for("AI_IDEMPOTENCY_REUSED"),
        )
    _log_ai_idempotency_claim(
        key=key,
        fingerprint=fingerprint,
        record=record,
        is_owner=is_owner,
        force_refresh=payload.forceRefresh,
    )

    if not is_owner:
        # Duplicate / replay: do not consume quota, do not take an AI slot; wait
        # for the owner and reuse its result.
        await _run_sync(db.rollback)
        result, error = await _wait_for_ai_follower_request_async(key, record, deadline_at=deadline_at)
        if result is not None:
            return AnalyzeResponse(**result)
        _ai_event("dedup", "reused", outcome="failed", timeout_stage=(error or {}).get("timeoutStage"))
        return AnalyzeResponse(**_reliability_error_response(error, payload.text))

    try:
        await _run_sync(
            check_daily_quota,
            db,
            user_id=current_user.id,
            resource="ai",
            limit=settings.ai_daily_quota,
        )
        log_event(logger, logging.INFO, "quota_precheck", resource="ai", outcome="passed")
        log_event(logger, logging.INFO, "ai_slot_wait_start", resource="ai")
        async with async_resource_slot("ai", _slot_timeout(deadline_at)):
            log_event(logger, logging.INFO, "ai_slot_acquired", resource="ai")
            await _run_sync(
                consume_daily_quota,
                db,
                user_id=current_user.id,
                resource="ai",
                limit=settings.ai_daily_quota,
            )
            log_event(logger, logging.INFO, "quota_committed", resource="ai", increment=1)
            log_event(logger, logging.INFO, "ai_analyze_start", resource="ai", provider="ollama", model=settings.ollama_model)
            result = await _run_sync(
                _analyze_text_observed_sync,
                payload,
                deadline_at=deadline_at,
                record=record,
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            code, stage = "AI_DAILY_QUOTA", "quota"
        else:
            code, stage = "AI_QUEUE_FULL", "slot"
        _ai_event("failure", code, status_code=exc.status_code)
        finish_ai_request(
            key,
            record,
            error={
                "code": code,
                "message": user_message_for(code),
                "timeoutStage": stage,
            },
        )
        raise
    except Exception as exc:
        _ai_event("failure", "AI_INTERNAL_ERROR", error_type=type(exc).__name__)
        finish_ai_request(
            key,
            record,
            error={
                "code": "AI_INTERNAL_ERROR",
                "message": user_message_for("AI_INTERNAL_ERROR"),
            },
        )
        raise

    finish_ai_request(key, record, result=result)
    return AnalyzeResponse(**result)


async def _bridge_sync_generator(
    sync_gen: Iterator[tuple],
    worker_ctx: contextvars.Context,
    *,
    cancel_controller: StreamCancelController,
) -> AsyncIterator[tuple]:
    """Bridge a blocking sync generator onto the event loop under one context.

    Starlette's default StreamingResponse path iterates sync generators via
    ``anyio.to_thread.run_sync`` *per item*, which copies the context for every
    ``next()``/``close()`` call. That per-item context churn breaks OpenTelemetry's
    contextvar token lifecycle (``Failed to detach context``) and hands the
    cancelled generator a stale or ``None`` request_id. This helper instead drives
    the whole generator on one dedicated worker thread under a captured
    ``worker_ctx`` and forwards events over a thread-safe queue, waking the event
    loop via ``loop.call_soon_threadsafe`` (no polling).

    Cancellation: the bridge's ``finally`` calls ``cancel_controller.cancel()``,
    which closes the Ollama response and therefore unblocks the worker's blocking
    read; the worker observes the cancelled flag and exits on its own. The caller
    releases the AI slot in its own ``finally`` immediately after, so a disconnect
    does not have to wait for the abandoned generator to be GC'd.
    """
    _sentinel = object()
    events: queue.SimpleQueue = queue.SimpleQueue()
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal() -> None:
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass  # event loop is shutting down

    def _run() -> None:
        try:
            while True:
                try:
                    item = worker_ctx.run(next, sync_gen)
                except StopIteration:
                    events.put(_sentinel)
                    _signal()
                    return
                events.put(item)
                _signal()
        except BaseException as exc:  # noqa: BLE001 - forward GeneratorExit/errors
            events.put(exc)
            _signal()

    worker = threading.Thread(target=_run, name="ollama-stream-bridge", daemon=True)
    worker.start()

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
            if not events.empty():  # signal landed between the drain and the clear
                continue
            await wake.wait()
    finally:
        cancel_controller.cancel()
        # Non-blocking aliveness peek: the AI slot is released by the caller's
        # own finally, so we must NOT stall this close with a blocking join.
        # A still-running worker is only informational (it is a daemon and
        # exits on its own once its in-flight Ollama POST returns).
        if worker.is_alive():
            logger.warning("[stream][bridge] worker still alive after cancel()")


@app.post("/api/analyze-english/stream")
async def analyze_english_stream(
    payload: AnalyzeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream provisional qwen3:8b text and the authoritative result as NDJSON.

    The stream contains ``start``, provisional ``delta`` events, completed
    ``field`` snapshots, an optional strict-retry ``reset``, the validated
    ``final`` AnalyzeResponse, and ``done``.

    ``final.data`` is the exact same object the non-streaming endpoint returns
    (same validation / normalization / build path, serialized via AnalyzeResponse).
    """
    request_started_at = time.perf_counter()
    enforce_resource_rate_limit(request, current_user.id, "ai")
    deadline_at = total_timeout_deadline(settings.ai_total_timeout_seconds)
    idempotency_key = (
        request.headers.get("idempotency-key")
        if _valid_idempotency_key(request.headers.get("idempotency-key"))
        else None
    )
    fingerprint = _ai_request_fingerprint(payload)
    log_event(
        logger,
        logging.INFO,
        "stream_request_enter",
        operation="analyze_english_stream",
        idempotency_key=idempotency_key or "",
        fingerprint=fingerprint[:12],
        force_refresh=bool(payload.forceRefresh),
        process_id=os.getpid(),
        working_directory=os.getcwd(),
        app_main_file=os.path.abspath(__file__),
        request_reliability_file=os.path.abspath(request_reliability_module.__file__),
        module_loaded_at=APP_MODULE_LOADED_AT,
        diagnostic_version=STREAM_DIAGNOSTIC_VERSION,
    )

    try:
        key, record, is_owner = claim_ai_request(current_user.id, idempotency_key, fingerprint)
    except IdempotencyKeyReuseError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=user_message_for("AI_IDEMPOTENCY_REUSED"),
        )
    _log_ai_idempotency_claim(
        key=key,
        fingerprint=fingerprint,
        record=record,
        is_owner=is_owner,
        force_refresh=payload.forceRefresh,
    )

    if not is_owner:
        # Duplicate / replay: do not consume quota, do not take an AI slot; wait
        # for the owner and replay its result as a minimal NDJSON stream.
        await _run_sync(db.rollback)
        result, error = await _wait_for_ai_follower_request_async(key, record, deadline_at=deadline_at)
        if result is not None:
            _ai_event("dedup", "reused", outcome="success")
            data = AnalyzeResponse(**result).model_dump(mode="json")
        else:
            _ai_event("dedup", "reused", outcome="failed", timeout_stage=(error or {}).get("timeoutStage"))
            data = _reliability_error_response(error, payload.text)
        replay_attempt = max(1, int(getattr(record, "generation_attempts", 0) or 0))
        log_event(
            logger,
            logging.INFO,
            "ai_cache_lookup",
            operation="analyze_english_stream",
            result="not_checked",
            reason="idempotency_replay",
        )
        log_event(
            logger,
            logging.INFO,
            "ollama_generation_start",
            operation="analyze_english_stream",
            result=False,
            reason="idempotency_replay",
            attempt=replay_attempt,
        )
        log_event(
            logger,
            logging.INFO,
            "ai_stream_replay",
            operation="analyze_english_stream",
            result="success" if result is not None else "error",
            attempt=replay_attempt,
            delta_count=0,
            field_count=0,
            final_count=1,
            done_count=1,
        )
        return StreamingResponse(
            _ndjson_final_stream(data, attempt=replay_attempt),
            media_type="application/x-ndjson",
            headers={"Content-Encoding": "identity"},
        )

    slot_entered = False
    try:
        await _run_sync(
            check_daily_quota,
            db,
            user_id=current_user.id,
            resource="ai",
            limit=settings.ai_daily_quota,
        )
        log_event(logger, logging.INFO, "quota_precheck", resource="ai", outcome="passed")
        # Acquire the AI slot before reserving quota, then hold it for the stream.
        log_event(logger, logging.INFO, "ai_slot_wait_start", resource="ai")
        slot = async_resource_slot("ai", _slot_timeout(deadline_at))
        await slot.__aenter__()
        slot_entered = True
        log_event(
            logger,
            logging.INFO,
            "ai_slot_acquired",
            resource="ai",
            idempotency_key=key or "",
            record_id=hex(id(record)) if record is not None else "",
        )
        await _run_sync(
            consume_daily_quota,
            db,
            user_id=current_user.id,
            resource="ai",
            limit=settings.ai_daily_quota,
        )
        log_event(logger, logging.INFO, "quota_committed", resource="ai", increment=1)
        log_event(logger, logging.INFO, "ai_analyze_start", resource="ai", provider="ollama", model=settings.ollama_model)
    except HTTPException as exc:
        if slot_entered:
            await slot.__aexit__(type(exc), exc, exc.__traceback__)
            log_event(logger, logging.INFO, "ai_slot_released", resource="ai", result="setup_error")
        if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            code, stage = "AI_DAILY_QUOTA", "quota"
        else:
            code, stage = "AI_QUEUE_FULL", "slot"
        _ai_event("failure", code, status_code=exc.status_code)
        _finish_stream_ai_request(
            key,
            record,
            error={
                "code": code,
                "message": user_message_for(code),
                "timeoutStage": stage,
            },
        )
        raise
    except Exception as exc:
        if slot_entered:
            await slot.__aexit__(type(exc), exc, exc.__traceback__)
            log_event(logger, logging.INFO, "ai_slot_released", resource="ai", result="setup_error")
        _ai_event("failure", "AI_INTERNAL_ERROR", error_type=type(exc).__name__)
        _finish_stream_ai_request(
            key,
            record,
            error={
                "code": "AI_INTERNAL_ERROR",
                "message": user_message_for("AI_INTERNAL_ERROR"),
            },
        )
        raise

    # The blocking analyzer is a sync generator; drive it on a dedicated worker
    # thread under a captured context (request_id / OTel vars) so every resumed
    # event is produced with the same request_id and the OTel span token detaches
    # cleanly. A disconnect cancels this async generator, whose finally closes the
    # Ollama response (via the controller) and releases the AI slot immediately.
    cancel_controller = StreamCancelController()
    worker_ctx = contextvars.copy_context()
    sync_gen = analyze_text_streaming(
        text=payload.text,
        card_type=payload.cardType,
        target_lang=payload.targetLang,
        force_refresh=payload.forceRefresh,
        deadline_at=deadline_at,
        record=record,
        cancel_controller=cancel_controller,
    )

    async def ndjson_stream():
        t_start = time.perf_counter()
        first_delta_at: float | None = None
        first_field_at: float | None = None
        stream_result = "success"
        final_payload: dict | None = None
        delta_count = 0
        field_count = 0
        final_count = 0
        done_count = 0
        try:
            with observed_operation(
                "ai",
                "analyze_english_stream",
                attributes={
                    "card_type": payload.cardType,
                    "target_lang": payload.targetLang,
                    "force_refresh": payload.forceRefresh,
                },
            ):
                yield json.dumps({"type": "start", "attempt": 1}, ensure_ascii=False) + "\n"
                async for event in _bridge_sync_generator(
                    sync_gen,
                    worker_ctx,
                    cancel_controller=cancel_controller,
                ):
                    kind = event[0]
                    if kind == "delta":
                        delta_count += 1
                        attempt = event[4] if len(event) > 4 else 1
                        log_event(
                            logger,
                            logging.INFO,
                            "fastapi_delta",
                            operation="analyze_english_stream",
                            seq=event[3],
                            field=event[1],
                            attempt=attempt,
                            delta_count=delta_count,
                        )
                        if first_delta_at is None:
                            first_delta_at = time.perf_counter()
                            log_event(
                                logger,
                                logging.INFO,
                                "first_fastapi_delta",
                                operation="analyze_english_stream",
                                field=event[1],
                                seq=event[3],
                                attempt=attempt,
                                duration_ms=round((first_delta_at - request_started_at) * 1000, 3),
                            )
                        yield (
                            json.dumps(
                                {
                                    "type": "delta",
                                    "field": event[1],
                                    "text": event[2],
                                    "seq": event[3],
                                    "attempt": attempt,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    elif kind == "field":
                        field_count += 1
                        attempt = event[3] if len(event) > 3 else 1
                        log_event(
                            logger,
                            logging.INFO,
                            "fastapi_field",
                            operation="analyze_english_stream",
                            field=event[1],
                            attempt=attempt,
                            field_count=field_count,
                        )
                        if first_field_at is None:
                            first_field_at = time.perf_counter()
                            log_event(
                                logger,
                                logging.INFO,
                                "ai_stream_first_field",
                                operation="analyze_english_stream",
                                field=event[1],
                                duration_ms=round((first_field_at - t_start) * 1000, 3),
                            )
                        yield (
                            json.dumps(
                                {
                                    "type": "field",
                                    "field": event[1],
                                    "value": event[2],
                                    "attempt": attempt,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    elif kind == "reset":
                        attempt = event[1] if len(event) > 1 else 2
                        log_event(
                            logger,
                            logging.INFO,
                            "fastapi_reset",
                            operation="analyze_english_stream",
                            attempt=attempt,
                            delta_count=delta_count,
                            field_count=field_count,
                        )
                        yield json.dumps(
                            {"type": "reset", "attempt": attempt},
                            ensure_ascii=False,
                        ) + "\n"
                    elif kind == "final":
                        final_count += 1
                        attempt = event[2] if len(event) > 2 else 1
                        data = AnalyzeResponse(**event[1]).model_dump(mode="json")
                        final_payload = data
                        if not data.get("ok", False):
                            stream_result = "error"
                        log_event(
                            logger,
                            logging.INFO if stream_result == "success" else logging.WARNING,
                            "ai_stream_final",
                            operation="analyze_english_stream",
                            result=stream_result,
                            attempt=attempt,
                            delta_count=delta_count,
                            field_count=field_count,
                            final_count=final_count,
                            duration_ms=round((time.perf_counter() - t_start) * 1000, 3),
                            request_duration_ms=round((time.perf_counter() - request_started_at) * 1000, 3),
                        )
                        yield (
                            json.dumps(
                                {"type": "final", "data": data, "attempt": attempt},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        break
        except (
            anyio.get_cancelled_exc_class(),
            GeneratorExit,
            BrokenPipeError,
            ConnectionResetError,
        ):
            stream_result = "cancelled"
            _ai_event("cancelled", "cancelled", stage="client_disconnect")
            log_event(
                logger,
                logging.WARNING,
                "ai_stream_cancelled",
                operation="analyze_english_stream",
                result=stream_result,
                delta_count=delta_count,
                field_count=field_count,
                final_count=final_count,
                duration_ms=round((time.perf_counter() - t_start) * 1000, 3),
            )
            # If the final was already emitted, the analysis itself succeeded and
            # the record should carry that result so a replay dedups; otherwise
            # the record records a cancellation.
            if final_payload is not None:
                _finish_stream_ai_request(key, record, result=final_payload)
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "ai_cache_write",
                    operation="analyze_english_stream",
                    result="skipped",
                    reason="client_cancelled_before_final",
                )
                _finish_stream_ai_request(
                    key,
                    record,
                    error={
                        "code": "AI_CANCELLED",
                        "message": user_message_for("AI_CANCELLED"),
                        "timeoutStage": "client_disconnect",
                    },
                )
            raise
        except Exception as exc:
            _ai_event("failure", "AI_INTERNAL_ERROR", error_type=type(exc).__name__)
            _finish_stream_ai_request(
                key,
                record,
                error={
                    "code": "AI_INTERNAL_ERROR",
                    "message": user_message_for("AI_INTERNAL_ERROR"),
                },
            )
            raise
        finally:
            await slot.__aexit__(None, None, None)
            log_event(
                logger,
                logging.INFO,
                "ai_slot_released",
                resource="ai",
                result=stream_result,
                delta_count=delta_count,
                field_count=field_count,
                final_count=final_count,
            )

        if final_payload is None:
            # A clean iterator exit without a final event is still a broken
            # stream protocol. Complete the idempotency record as an error and
            # deliberately omit ``done`` so the client can use the same key for
            # direct replay without starting another Qwen generation.
            _ai_event("failure", "AI_STREAM_INCOMPLETE", stage="missing_final")
            _finish_stream_ai_request(
                key,
                record,
                error={
                    "code": "AI_INTERNAL_ERROR",
                    "message": user_message_for("AI_INTERNAL_ERROR"),
                    "timeoutStage": "stream_missing_final",
                },
            )
            return

        _finish_stream_ai_request(key, record, result=final_payload)
        done_count += 1
        log_event(
            logger,
            logging.INFO,
            "ai_stream_done",
            operation="analyze_english_stream",
            result=stream_result,
            delta_count=delta_count,
            field_count=field_count,
            final_count=final_count,
            done_count=done_count,
            duration_ms=round((time.perf_counter() - t_start) * 1000, 3),
        )
        yield json.dumps({"type": "done"}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        ndjson_stream(),
        media_type="application/x-ndjson",
        headers={"Content-Encoding": "identity"},
    )
