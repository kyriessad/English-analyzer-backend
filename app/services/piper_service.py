from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings
from app.observability.logging import log_event
from app.observability.metrics import TTS_CACHE_EVENTS_TOTAL
from app.observability.operations import observed_operation, record_operation_result


logger = logging.getLogger(__name__)


class PronunciationError(RuntimeError):
    pass


_VALID_VOICES = {"male", "female"}
_voice_cache: dict[str, object] = {}
_voice_cache_lock = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()
_EDGE_SPACE_RE = re.compile(r"\s+")


def normalize_pronunciation_text(text: str) -> str:
    normalized = _EDGE_SPACE_RE.sub(" ", str(text or "").strip())
    if len(normalized) > settings.piper_max_text_chars:
        raise PronunciationError(
            f"Text is too long. Maximum is {settings.piper_max_text_chars} characters."
        )
    return normalized


def _resolve_voice_name(voice: str | None) -> str:
    """Resolve 'male' / 'female' / None to the configured Piper model name."""
    resolved = (voice or "").strip().lower()
    if not resolved:
        resolved = settings.piper_default_voice
    if resolved not in _VALID_VOICES:
        raise PronunciationError(
            f"Invalid voice '{voice}'. Supported values: male, female."
        )
    if resolved == "male":
        return settings.piper_male_voice
    return settings.piper_female_voice


def _voice_paths(voice: str | None) -> tuple[str, Path, Path]:
    """Return (piper_voice_name, model_path, config_path) for the given voice."""
    piper_voice_name = _resolve_voice_name(voice)
    voice_dir = Path(settings.piper_data_dir)
    model_path = voice_dir / f"{piper_voice_name}.onnx"
    config_path = voice_dir / f"{piper_voice_name}.onnx.json"
    return piper_voice_name, model_path, config_path


def pronunciation_available(voice: str | None = None) -> bool:
    """Check if voice model files exist. When voice is None, checks the default voice."""
    _, model_path, config_path = _voice_paths(voice)
    return model_path.is_file() and config_path.is_file()


def pronunciation_available_all() -> dict[str, bool]:
    """Check availability of both male and female voices."""
    return {
        "male": pronunciation_available("male"),
        "female": pronunciation_available("female"),
    }


def _load_voice(voice: str | None):
    started = time.perf_counter()
    piper_voice_name, model_path, config_path = _voice_paths(voice)
    cache_key = piper_voice_name

    if cache_key in _voice_cache:
        record_operation_result("tts", "piper_voice_cache", "hit", time.perf_counter() - started)
        return _voice_cache[cache_key]

    if not model_path.is_file() or not config_path.is_file():
        raise PronunciationError(
            f"Piper voice assets for '{piper_voice_name}' are missing. "
            "Run setup-english-language-assets.ps1 first."
        )

    with _voice_cache_lock:
        if cache_key in _voice_cache:
            record_operation_result("tts", "piper_voice_cache", "hit", time.perf_counter() - started)
            return _voice_cache[cache_key]

        with observed_operation("tts", "piper_load_voice", attributes={"voice": piper_voice_name}):
            try:
                from piper.voice import PiperVoice
            except Exception as exc:  # pragma: no cover - depends on local optional asset setup
                raise PronunciationError(
                    "piper-tts is not installed. Run setup-english-language-assets.ps1 first."
                ) from exc

            voice_obj = PiperVoice.load(str(model_path), config_path=str(config_path))
        _voice_cache[cache_key] = voice_obj
        record_operation_result("tts", "piper_voice_cache", "miss", time.perf_counter() - started)
        return voice_obj


def warmup_voices() -> dict[str, str]:
    """Preload both Piper voices into the in-process ``_voice_cache``.

    Returns ``{voice: status}`` where ``voice`` is ``"female"`` / ``"male"``
    and ``status`` is ``"ok"`` on success or an error description on failure.
    Called once at FastAPI startup so the first real TTS request does not pay
    the model-load latency.
    """
    result: dict[str, str] = {}
    for voice in ("female", "male"):
        try:
            _load_voice(voice)
        except Exception as exc:  # pragma: no cover - depends on optional local assets
            result[voice] = f"{type(exc).__name__}: {exc}"
        else:
            result[voice] = "ok"
    return result


def _cache_key(text: str, voice: str | None) -> str:
    piper_voice_name = _resolve_voice_name(voice)
    payload = {
        "text": text,
        "voice": piper_voice_name,
        "format": "wav",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _get_cache_lock(key: str) -> threading.Lock:
    with _cache_locks_guard:
        lock = _cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[key] = lock
        return lock


def _prune_lock_registry() -> None:
    with _cache_locks_guard:
        if len(_cache_locks) <= 4096:
            return
        removable = [key for key, lock in _cache_locks.items() if not lock.locked()]
        for key in removable[: len(_cache_locks) - 4096]:
            _cache_locks.pop(key, None)


def prune_audio_cache(protected_path: Path | None = None) -> None:
    started = time.perf_counter()
    cache_dir = Path(settings.piper_audio_cache_dir)
    if not cache_dir.is_dir():
        record_operation_result("tts", "wav_cache_prune", "skipped", time.perf_counter() - started)
        return
    wav_files = [
        path
        for path in cache_dir.glob("*.wav")
        if path.is_file() and (protected_path is None or path != protected_path)
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, settings.piper_cache_max_age_days))
    for path in list(wav_files):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            wav_files.remove(path)

    total = sum(path.stat().st_size for path in wav_files)
    max_bytes = max(1, settings.piper_cache_max_bytes)
    if total > max_bytes:
        for path in sorted(wav_files, key=lambda item: item.stat().st_mtime):
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            if total <= max_bytes:
                break
    _prune_lock_registry()
    record_operation_result("tts", "wav_cache_prune", "success", time.perf_counter() - started)


def get_cached_audio(text: str, voice: str | None = None) -> Path | None:
    """Return an existing valid WAV without entering the Piper execution queue."""
    started = time.perf_counter()
    normalized_text = normalize_pronunciation_text(text)
    if not normalized_text:
        raise PronunciationError("Text is required.")

    key = _cache_key(normalized_text, voice)
    cache_path = Path(settings.piper_audio_cache_dir) / f"{key}.wav"
    if not cache_path.is_file() or cache_path.stat().st_size <= 44:
        return None

    TTS_CACHE_EVENTS_TOTAL.labels(operation="wav_cache", result="hit").inc()
    record_operation_result("tts", "wav_cache", "hit", time.perf_counter() - started)
    prune_audio_cache(protected_path=cache_path)
    return cache_path


def synthesize_or_get_cached_audio(text: str, voice: str | None = None) -> Path:
    started = time.perf_counter()
    normalized_text = normalize_pronunciation_text(text)
    if not normalized_text:
        raise PronunciationError("Text is required.")

    resolved_voice = (voice or "").strip().lower()
    if not resolved_voice:
        resolved_voice = settings.piper_default_voice
    if resolved_voice not in _VALID_VOICES:
        raise PronunciationError(
            f"Invalid voice '{voice}'. Supported values: male, female."
        )

    cached_path = get_cached_audio(normalized_text, resolved_voice)
    if cached_path is not None:
        return cached_path

    cache_dir = Path(settings.piper_audio_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    key = _cache_key(normalized_text, resolved_voice)
    cache_path = cache_dir / f"{key}.wav"

    lock = _get_cache_lock(key)
    with lock:
        if cache_path.is_file() and cache_path.stat().st_size > 44:
            TTS_CACHE_EVENTS_TOTAL.labels(operation="wav_cache", result="hit_after_lock").inc()
            record_operation_result("tts", "wav_cache", "hit_after_lock", time.perf_counter() - started)
            return cache_path

        TTS_CACHE_EVENTS_TOTAL.labels(operation="wav_cache", result="miss").inc()
        voice_obj = _load_voice(resolved_voice)
        temp_path = cache_dir / f".{key}.{uuid.uuid4().hex}.tmp"
        try:
            log_event(
                logger,
                logging.INFO,
                "tts_piper_generation_started",
                resource="tts",
                voice=resolved_voice,
                temp_file_path=str(temp_path),
                final_file_path=str(cache_path),
            )
            with observed_operation("tts", "piper_synthesize", attributes={"voice": resolved_voice}):
                with wave.open(str(temp_path), "wb") as wav_file:
                    voice_obj.synthesize_wav(normalized_text, wav_file)
            os.replace(temp_path, cache_path)
            file_exists = cache_path.is_file()
            log_event(
                logger,
                logging.INFO,
                "tts_piper_generation_completed",
                resource="tts",
                voice=resolved_voice,
                generated_file_path=str(cache_path),
                file_exists=file_exists,
                file_size=cache_path.stat().st_size if file_exists else 0,
            )
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            if isinstance(exc, PronunciationError):
                raise
            raise PronunciationError("Piper failed to synthesize audio.") from exc

    prune_audio_cache(protected_path=cache_path)
    record_operation_result("tts", "wav_cache", "generated", time.perf_counter() - started)
    return cache_path
