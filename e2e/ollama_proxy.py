"""Transparent E2E-only Ollama proxy with a filesystem failure switch."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse


app = FastAPI(title="Level 7 E2E Ollama transport")
_client: httpx.AsyncClient | None = None
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _upstream() -> str:
    return os.environ.get("E2E_OLLAMA_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")


def _failure_switch() -> Path:
    raw = os.environ.get("E2E_OLLAMA_FAILURE_SWITCH", "")
    if not raw:
        raise RuntimeError("E2E_OLLAMA_FAILURE_SWITCH is required")
    return Path(raw)


@app.on_event("startup")
async def startup() -> None:
    global _client
    _failure_switch()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))


@app.on_event("shutdown")
async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@app.get("/__e2e/proxy-health")
async def proxy_health() -> dict[str, object]:
    return {
        "status": "ok",
        "upstream": _upstream(),
        "failure_enabled": _failure_switch().exists(),
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def forward(path: str, request: Request) -> Response:
    if _failure_switch().exists():
        return Response(
            content=b'{"error":"E2E_OLLAMA_TRANSPORT_UNAVAILABLE"}',
            status_code=503,
            media_type="application/json",
        )
    if _client is None:
        return Response(status_code=503)

    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() not in {"host", "content-length"}
    }
    upstream_request = _client.build_request(
        request.method,
        f"{_upstream()}/{path}",
        params=request.query_params,
        headers=headers,
        content=body,
    )
    upstream_response = await _client.send(upstream_request, stream=True)
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"
    }
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_response.aclose),
    )
