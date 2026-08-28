from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.user import User
from app.services.ecdict_service import (
    get_phonetic,
    get_word_phonetics,
    normalize_lexical_text,
)
from app.services.piper_service import (
    PronunciationError,
    get_cached_audio,
    normalize_pronunciation_text,
    pronunciation_available,
    pronunciation_available_all,
    synthesize_or_get_cached_audio,
)
from app.services.auth_service import get_current_user
from app.services.security import (
    async_resource_slot,
    consume_daily_quota,
    enforce_resource_rate_limit,
)
from app.observability.logging import log_event
from app.services.runtime_diagnostics import add_client_log, utc_timestamp


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api", tags=["language"])


class WordPhoneticItem(BaseModel):
    word: str
    phonetic: str | None = None


class LexicalInfoResponse(BaseModel):
    text: str
    phonetic: str | None = None
    phoneticSource: str | None = None
    wordPhonetics: list[WordPhoneticItem] | None = None
    pronunciationAvailable: bool


class TtsClientDiagnosticRequest(BaseModel):
    event: str
    requestId: str | None = None
    timestamp: str | None = None
    details: dict[str, Any] | None = None
    client: dict[str, Any] | None = None


def _short_string(value: Any, max_length: int = 1000) -> str:
    return str(value or "")[:max_length]


def _safe_dict(value: dict[str, Any] | None, max_items: int = 30) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, item in list((value or {}).items())[:max_items]:
        key_text = _short_string(key, 80)
        lowered = key_text.lower()
        if "token" in lowered or "secret" in lowered or "authorization" in lowered:
            safe[key_text] = "[redacted]"
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[key_text] = item if not isinstance(item, str) else item[:1000]
        else:
            safe[key_text] = _short_string(item, 1000)
    return safe


@router.get("/lexical-info", response_model=LexicalInfoResponse)
def get_lexical_info(
    request: Request,
    text: str = Query(min_length=1, max_length=300),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LexicalInfoResponse:
    enforce_resource_rate_limit(request, current_user.id, "lexical")
    consume_daily_quota(
        db,
        user_id=current_user.id,
        resource="lexical",
        limit=settings.lexical_daily_quota,
    )
    normalized_text = normalize_lexical_text(text)

    # Priority 1: exact full-text match
    phonetic = get_phonetic(normalized_text)

    # Priority 2: word-by-word fallback for multi-word phrases
    word_phonetics = None
    if not phonetic:
        words = normalized_text.split()
        if len(words) > 1:
            wp = get_word_phonetics(normalized_text)
            if wp:
                word_phonetics = [WordPhoneticItem(**item) for item in wp]

    return LexicalInfoResponse(
        text=normalized_text,
        phonetic=phonetic,
        phoneticSource="ecdict" if phonetic else None,
        wordPhonetics=word_phonetics,
        pronunciationAvailable=pronunciation_available(),
    )


@router.post("/diagnostics/tts-client")
def record_tts_client_diagnostic(
    payload: TtsClientDiagnosticRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    record = {
        "timestamp": utc_timestamp(),
        "clientTimestamp": _short_string(payload.timestamp, 80),
        "source": "tts-client",
        "event": _short_string(payload.event, 120),
        "requestId": _short_string(payload.requestId, 128),
        "serverRequestId": _short_string(getattr(getattr(request, "state", None), "request_id", ""), 128),
        "userAgent": request.headers.get("user-agent", "")[:512],
        "userId": current_user.id,
        "details": _safe_dict(payload.details),
        "client": _safe_dict(payload.client),
    }
    add_client_log(record)
    log_event(
        logger,
        logging.INFO,
        "tts_client_diagnostic_received",
        request_id=record["requestId"],
        client_event=record["event"],
    )
    return {"ok": True}


@router.get("/diagnostics/test-audio.mp3")
def get_tts_diagnostic_test_audio(
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    audio_path = Path(__file__).resolve().parents[2] / "data" / "diagnostics" / "test-tone.mp3"
    if not audio_path.is_file():
        raise HTTPException(status_code=503, detail="diagnostic test audio is not available")
    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename="tts-diagnostic-test-tone.mp3",
    )


@router.get("/pronunciation/audio")
async def get_pronunciation_audio(
    request: Request,
    text: str = Query(min_length=1, max_length=300),
    voice: str | None = Query(default=None, pattern=r"^(male|female)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    request_state = getattr(request, "state", None)
    request_id = getattr(request_state, "request_id", None)
    log_event(
        logger,
        logging.INFO,
        "tts_request_received",
        resource="tts",
        request_id=request_id,
        voice=voice or "",
    )
    enforce_resource_rate_limit(request, current_user.id, "tts")
    await anyio.to_thread.run_sync(
        partial(
            consume_daily_quota,
            db,
            user_id=current_user.id,
            resource="tts",
            limit=settings.tts_daily_quota,
        )
    )
    try:
        normalized_text = normalize_pronunciation_text(text)
        audio_path = await anyio.to_thread.run_sync(
            partial(get_cached_audio, normalized_text, voice=voice)
        )
        cache_hit = audio_path is not None
        if audio_path is None:
            async with async_resource_slot("tts"):
                audio_path = await anyio.to_thread.run_sync(
                    partial(synthesize_or_get_cached_audio, normalized_text, voice=voice)
                )
    except PronunciationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    file_path = Path(audio_path)
    file_exists = file_path.is_file()
    file_size = file_path.stat().st_size if file_exists else 0
    log_event(
        logger,
        logging.INFO,
        "tts_response_ready",
        resource="tts",
        request_id=request_id,
        cache_hit=cache_hit,
        generated_file_path=str(file_path),
        file_exists=file_exists,
        file_size=file_size,
        response_status=200,
        content_type="audio/wav",
        content_length=file_size,
    )
    return FileResponse(
        path=str(audio_path),
        media_type="audio/wav",
        filename="pronunciation.wav",
    )
