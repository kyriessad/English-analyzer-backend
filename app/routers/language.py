from __future__ import annotations

from functools import partial

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


@router.get("/pronunciation/audio")
async def get_pronunciation_audio(
    request: Request,
    text: str = Query(min_length=1, max_length=300),
    voice: str | None = Query(default=None, pattern=r"^(male|female)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
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
        if audio_path is None:
            async with async_resource_slot("tts"):
                audio_path = await anyio.to_thread.run_sync(
                    partial(synthesize_or_get_cached_audio, normalized_text, voice=voice)
                )
    except PronunciationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return FileResponse(
        path=str(audio_path),
        media_type="audio/wav",
        filename="pronunciation.wav",
    )
