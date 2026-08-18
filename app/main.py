from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import (
    SessionLocal,
    assert_expected_database,
    get_database_runtime_info,
    get_db,
)
from app.models.user import User
from app.routers.auth import router as auth_router
from app.routers.cards import router as cards_router
from app.routers.language import router as language_router
from app.routers.reviews import review_sessions_router, router as reviews_router
from app.schemas import AnalyzeRequest, AnalyzeResponse
from app.observability.context import reset_request_id, set_request_id
from app.observability.errors import classify_exception, result_from_status
from app.observability.logging import configure_logging, log_event
from app.observability.metrics import (
    AI_REQUEST_DURATION_SECONDS,
    AI_REQUESTS_TOTAL,
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
from app.services.security import (
    consume_daily_quota,
    enforce_resource_rate_limit,
    resource_slot,
)
from app.services.runtime_diagnostics import add_request_log, utc_timestamp


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
configure_logging()
configure_tracing()
logger = logging.getLogger(__name__)


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
    database_info = get_database_runtime_info()
    logger.info("Database dialect: %s", database_info.dialect)
    logger.info("Database host: %s", database_info.host)
    logger.info("Database port: %s", database_info.port)
    logger.info("Database name: %s", database_info.database)
    logger.info("Database schema: %s", database_info.schema)
    logger.info("Database current user: %s", database_info.current_user)
    logger.info("Database URL source: %s", database_info.url_source)
    logger.info(
        "Alembic revision: %s",
        ",".join(database_info.alembic_revisions) or "missing",
    )
    assert_expected_database(database_info)

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


@app.post("/api/analyze-english", response_model=AnalyzeResponse)
def analyze_english(
    payload: AnalyzeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalyzeResponse:
    enforce_resource_rate_limit(request, current_user.id, "ai")
    consume_daily_quota(
        db,
        user_id=current_user.id,
        resource="ai",
        limit=settings.ai_daily_quota,
    )
    with resource_slot("ai", settings.ai_queue_timeout_seconds):
        with observed_operation(
            "ai",
            "analyze_english",
            attributes={
                "card_type": payload.cardType,
                "target_lang": payload.targetLang,
                "force_refresh": payload.forceRefresh,
            },
        ):
            result = analyze_text(
                text=payload.text,
                card_type=payload.cardType,
                target_lang=payload.targetLang,
                force_refresh=payload.forceRefresh,
            )
    return AnalyzeResponse(**result)


@app.post("/api/analyze-english/stream")
def analyze_english_stream(
    payload: AnalyzeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream the single qwen3:8b analysis as NDJSON field events.

    Returns ``application/x-ndjson`` lines: ``{"type":"start"}``, then
    ``{"type":"field","field":...,"value":...}`` for each completed field, then
    ``{"type":"final","data":{...AnalyzeResponse...}}``, then ``{"type":"done"}``.

    ``final.data`` is the exact same object the non-streaming endpoint returns
    (same validation / normalization / build path, serialized via AnalyzeResponse).
    """
    enforce_resource_rate_limit(request, current_user.id, "ai")
    consume_daily_quota(
        db,
        user_id=current_user.id,
        resource="ai",
        limit=settings.ai_daily_quota,
    )

    # Acquire the AI concurrency slot synchronously so a full queue still returns
    # 503 before the response starts (matching the non-stream endpoint), then hold
    # it for the whole stream and release it when the stream finishes.
    slot = resource_slot("ai", settings.ai_queue_timeout_seconds)
    slot.__enter__()

    def ndjson_stream():
        t_start = time.perf_counter()
        first_field_at: float | None = None
        stream_result = "success"
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
                yield json.dumps({"type": "start"}, ensure_ascii=False) + "\n"
                for event in analyze_text_streaming(
                    text=payload.text,
                    card_type=payload.cardType,
                    target_lang=payload.targetLang,
                    force_refresh=payload.forceRefresh,
                ):
                    kind = event[0]
                    if kind == "field":
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
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    elif kind == "final":
                        data = AnalyzeResponse(**event[1]).model_dump(mode="json")
                        if not data.get("ok", False):
                            stream_result = "error"
                        log_event(
                            logger,
                            logging.INFO if stream_result == "success" else logging.WARNING,
                            "ai_stream_final",
                            operation="analyze_english_stream",
                            result=stream_result,
                            duration_ms=round((time.perf_counter() - t_start) * 1000, 3),
                        )
                        yield (
                            json.dumps(
                                {"type": "final", "data": data},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        break
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            stream_result = "cancelled"
            log_event(
                logger,
                logging.WARNING,
                "ai_stream_cancelled",
                operation="analyze_english_stream",
                result=stream_result,
                duration_ms=round((time.perf_counter() - t_start) * 1000, 3),
            )
            raise
        finally:
            slot.__exit__(None, None, None)
        yield json.dumps({"type": "done"}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        ndjson_stream(),
        media_type="application/x-ndjson",
        headers={"Content-Encoding": "identity"},
    )
