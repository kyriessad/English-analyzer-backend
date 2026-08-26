"""The production ASGI app plus tightly guarded E2E-only fault controls.

This module is never imported by the normal startup scripts.  The controls are
available only when ``APP_ENV=e2e`` and a per-run secret is supplied.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import Header, HTTPException, Query
from sqlalchemy import text

from app.database import SessionLocal
from app.main import app
from app.services import piper_service


def _require_control_token(value: str | None) -> None:
    expected = os.environ.get("E2E_CONTROL_TOKEN", "")
    if os.environ.get("APP_ENV") != "e2e" or not expected or value != expected:
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/__e2e/http-hold", include_in_schema=False)
async def e2e_http_hold(
    seconds: float = Query(default=1.0, ge=0.05, le=15.0),
    x_e2e_control_token: str | None = Header(default=None),
) -> dict[str, float]:
    """Hold a real Uvicorn request without touching the application DB pool."""
    _require_control_token(x_e2e_control_token)
    await asyncio.sleep(seconds)
    return {"held_seconds": seconds}


@app.get("/__e2e/db-hold", include_in_schema=False)
def e2e_db_hold(
    seconds: float = Query(default=5.0, ge=0.1, le=30.0),
    x_e2e_control_token: str | None = Header(default=None),
) -> dict[str, float]:
    """Hold exactly one real SQLAlchemy pool checkout using PostgreSQL sleep."""
    _require_control_token(x_e2e_control_token)
    with SessionLocal() as db:
        try:
            db.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": seconds})
        finally:
            db.rollback()
    return {"held_seconds": seconds}


@app.post("/__e2e/piper-cache/clear", include_in_schema=False)
def e2e_clear_piper_voice_cache(
    x_e2e_control_token: str | None = Header(default=None),
) -> dict[str, int]:
    """Forget loaded voices so an isolated model-file failure can be injected."""
    _require_control_token(x_e2e_control_token)
    with piper_service._voice_cache_lock:
        cleared = len(piper_service._voice_cache)
        piper_service._voice_cache.clear()
    return {"cleared": cleared}
