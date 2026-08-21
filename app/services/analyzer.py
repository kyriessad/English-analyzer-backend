"""
Main analysis flow for /api/analyze-english.
"""
import logging
import time
from typing import Any

from app.core.config import settings
from app.observability.logging import log_event
from app.observability.metrics import AI_CACHE_EVENTS_TOTAL, AI_REQUEST_EVENTS_TOTAL
from app.observability.operations import observed_operation, record_operation_result
from app.schemas import Category, Level
from app.services.cache import delete_cache, get_cache, make_cache_key, set_cache
from app.services.ecdict_service import get_dictionary_translation
from app.services.hunyuan_example import generate_example_with_hunyuan
from app.services.ollama_example import (
    generate_analysis_with_ollama,
    generate_analysis_with_ollama_stream,
    generate_example_with_ollama,
)
from app.services.request_reliability import (
    StreamCancelController,
    touch_generation_attempt,
    user_message_for,
)
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english

logger = logging.getLogger(__name__)


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"


def _ai_event(event: str, result: str, **extra: Any) -> None:
    """Emit one reliability event into both the Prometheus counter and the log stream."""
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


def _make_attempt_recorder(record: Any) -> Any:
    """Build the per-Qwen-request attempt recorder bound to a reliability record."""
    if record is None:
        return None

    def record_attempt() -> None:
        attempt = touch_generation_attempt(record)
        _ai_event("generation_attempt", "started", attempt=attempt)

    return record_attempt


def _is_word_or_phrase(category: str | None) -> bool:
    return category in ("word", "phrase")


def _has_example(response: dict[str, Any]) -> bool:
    return bool(str(response.get("exampleSentence") or "").strip())


def _generate_example_with_tmt(
    word: str,
    translation: str,
    category: str,
) -> tuple[str | None, str | None]:
    """
    Legacy optional TMT fallback.

    The file and function are kept for rollback, but the default local mode never
    reaches this unless ENABLE_TENCENT_TMT=true and the Hunyuan legacy provider is
    explicitly selected.
    """
    logger.info(
        "[tmt][diag] start | text=%r | category=%s | translation_hint=%r",
        word,
        category,
        (translation or "")[:40],
    )
    try:
        if not settings.enable_tencent_tmt:
            logger.info("[tmt][diag] skipped | ENABLE_TENCENT_TMT=false")
            return None, None

        from app.providers.tencent_translator import TencentTranslator

        translator = TencentTranslator()
        word_lower = word.lower().strip()
        primary_translation = (
            translation.split("；")[0].split(";")[0].split("，")[0].split(",")[0].strip()
        )
        if not primary_translation:
            logger.warning("[tmt][diag] fail_reason=tmt_fallback_failed | empty translation")
            return None, None

        for zh in [
            f"他非常{primary_translation}，每天都如此。",
            f"这让她感到{primary_translation}。",
            f"她把{primary_translation}这件事记在心里。",
        ]:
            try:
                en = translator.translate_to_en(zh)
                if en and word_lower in en.lower():
                    return en.strip(), zh
            except Exception:
                continue

        logger.warning("[tmt][diag] fail_reason=tmt_fallback_failed | all templates failed")
        return None, None
    except Exception:
        logger.exception("[tmt][diag] fail_reason=tmt_fallback_failed | setup failed")
        return None, None


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) > 160:
        message = message[:157] + "..."
    return f"{exc.__class__.__name__}: {message or 'no details'}"


def _generate_example(
    normalized_text: str,
    category: str,
    translation: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    logger.info("[example] input text: %s", normalized_text)

    if not _is_word_or_phrase(category):
        logger.info("[example] provider: none")
        logger.info("[example] result found: false")
        return None, None, None, "not_eligible"

    provider = settings.example_generator_provider
    if provider == "ollama":
        logger.info("[example] provider: ollama")
        try:
            example_sentence, example_translation = generate_example_with_ollama(
                normalized_text,
                translation,
            )
        except Exception as exc:
            logger.warning("[example] error: %s", _short_error(exc))
            example_sentence, example_translation = None, None

        found = bool(example_sentence)
        logger.info("[example] result found: %s", str(found).lower())
        if found:
            return example_sentence, example_translation, "ollama", None

        logger.info("[example] error: NotFound: ollama returned no usable example")
        return None, None, None, "not_found"

    if provider == "hunyuan":
        logger.info("[example] provider: hunyuan")
        if not settings.enable_hunyuan:
            logger.info("[hunyuan][diag] skipped | ENABLE_HUNYUAN=false")
            logger.info("[example] result found: false")
            logger.info("[example] error: DisabledProvider: ENABLE_HUNYUAN=false")
            return None, None, None, "hunyuan_disabled"

        try:
            example_sentence, example_translation = generate_example_with_hunyuan(
                normalized_text,
                translation,
            )
        except Exception as exc:
            logger.warning("[example] error: %s", _short_error(exc))
            example_sentence, example_translation = None, None

        if example_sentence:
            logger.info("[example] result found: true")
            return example_sentence, example_translation, "hunyuan", None

        if translation and settings.enable_tencent_tmt:
            logger.info("[example] provider: tmt")
            tmt_sentence, tmt_translation = _generate_example_with_tmt(
                normalized_text,
                translation,
                category,
            )
            tmt_found = bool(tmt_sentence)
            logger.info("[example] result found: %s", str(tmt_found).lower())
            if tmt_found:
                return tmt_sentence, tmt_translation, "tmt", None

        logger.info("[example] result found: false")
        logger.info("[example] error: NotFound: hunyuan returned no usable example")
        return None, None, None, "not_found"

    logger.warning("[analyzer] unsupported example generator provider: %s", provider)
    logger.info("[example] provider: %s", provider)
    logger.info("[example] result found: false")
    logger.info("[example] error: UnsupportedProvider: %s", provider)
    return None, None, None, "unsupported_provider"


def _build_response(
    *,
    ok: bool,
    level: Level,
    category: Category,
    normalized_text: str,
    translation: str | None = None,
    understanding: str | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    provider: str | None = None,
    cache_hit: bool = False,
    example_sentence: str | None = None,
    example_translation: str | None = None,
    example_source: str | None = None,
    example_error: str | None = None,
    synonyms: list[dict] | None = None,
    similar_phrases: list[dict] | None = None,
    expression_type: str | None = None,
    alternative_meanings: list[dict] | None = None,
    usage_scenario: str | None = None,
    dialogue: dict | None = None,
    analysis_source: str | None = None,
    analysis_model: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "level": level,
        "category": category,
        "normalizedText": normalized_text,
        "translation": translation,
        "understanding": understanding,
        "warnings": warnings or [],
        "errors": errors or [],
        "provider": provider,
        "cacheHit": cache_hit,
        "exampleSentence": example_sentence,
        "exampleTranslation": example_translation,
        "exampleSource": example_source,
        "exampleError": example_error,
        "synonyms": synonyms or [],
        "similarPhrases": similar_phrases or [],
        "expressionType": expression_type or "literal",
        "alternativeMeanings": alternative_meanings or [],
        "usageScenario": usage_scenario or "",
        "dialogue": dialogue or {"english": [], "chinese": []},
        "analysisSource": analysis_source,
        "analysisModel": analysis_model,
    }


def _finalize_analysis(
    *,
    category: str,
    cache_key: str,
    validation_level: Level,
    normalized_text: str,
    warnings: list[str],
    errors: list[str],
    ollama: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the full response from an already-obtained ``ollama`` analysis dict.

    Shared by the non-streaming and streaming paths so the final result goes
    through the exact same translation / example / understanding / build / cache
    logic in both.
    """
    synonyms: list[dict] = []
    similar_phrases: list[dict] = []
    expression_type: str = "literal"
    alternative_meanings: list[dict] = []
    usage_scenario: str = ""
    dialogue: dict = {"english": [], "chinese": []}

    # Translation (understanding) source: Ollama meaning first, then Argos fallback.
    if ollama and ollama.get("meaning"):
        provider = "ollama"
        translation = ollama["meaning"]
    else:
        translation_result = translate_to_zh(normalized_text)
        provider = translation_result.get("provider")
        translation = translation_result.get("translation")

        if not translation_result.get("ok") or not translation:
            warnings.append(TRANSLATION_UNAVAILABLE_WARNING)
            translation = None

    # Example sentence + translation + synonyms + similar phrases.
    if ollama:
        example_sentence = ollama.get("exampleSentence")
        example_translation = ollama.get("exampleTranslation")
        example_source = "ollama"
        example_error = None
        synonyms = ollama.get("synonyms") or []
        similar_phrases = ollama.get("similarPhrases") or []
        expression_type = ollama.get("expressionType") or "literal"
        alternative_meanings = ollama.get("alternativeMeanings") or []
        usage_scenario = ollama.get("usageScenario") or ""
        dialogue = ollama.get("dialogue") or {"english": [], "chinese": []}
    else:
        example_sentence, example_translation, example_source, example_error = _generate_example(
            normalized_text,
            category,
            translation,
        )

    if _is_word_or_phrase(category):
        dictionary_translation = get_dictionary_translation(
            normalized_text,
            example_translation,
        )
        if dictionary_translation:
            if dictionary_translation != translation:
                provider = "ecdict"
            translation = dictionary_translation
            warnings = [item for item in warnings if item != TRANSLATION_UNAVAILABLE_WARNING]

    understanding = generate_understanding(
        normalized_text,
        category,
        translation,
    )

    analysis_source = "ollama" if ollama else "fallback"
    analysis_model = settings.ollama_model if ollama else None

    response = _build_response(
        ok=True,
        level=validation_level,
        category=category,
        normalized_text=normalized_text,
        translation=translation,
        understanding=understanding,
        warnings=warnings,
        errors=[],
        provider=provider,
        cache_hit=False,
        example_sentence=example_sentence,
        example_translation=example_translation,
        example_source=example_source,
        example_error=example_error,
        synonyms=synonyms,
        similar_phrases=similar_phrases,
        expression_type=expression_type,
        alternative_meanings=alternative_meanings,
        usage_scenario=usage_scenario,
        dialogue=dialogue,
        analysis_source=analysis_source,
        analysis_model=analysis_model,
    )

    cacheable = bool(translation)
    if cacheable and not (_is_word_or_phrase(category) and not example_sentence):
        set_cache(cache_key, response)

    return response


def _validation_context(text: str) -> tuple[dict[str, Any], str, Level, str, list[str], list[str]]:
    """Run validation and return the pieces both analysis paths need."""
    validation = validate_english(text)
    category = validation["category"]
    validation_level = validation["level"]
    normalized_text = validation["normalizedText"]
    warnings = list(validation.get("warnings") or [])
    errors = list(validation.get("errors") or [])
    return validation, category, validation_level, normalized_text, warnings, errors


def analyze_text(
    text: str,
    card_type: str = "auto",
    target_lang: str = "zh",
    force_refresh: bool = False,
    *,
    deadline_at: float | None = None,
    record: Any | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result_label = "success"
    if deadline_at is None:
        deadline_at = time.monotonic() + settings.ai_total_timeout_seconds
    _ai_event(
        "deadline_set",
        "started",
        total_timeout_seconds=settings.ai_total_timeout_seconds,
        deadline_at=round(deadline_at, 3),
    )
    try:
        with observed_operation("ai", "validate_english"):
            validation, category, validation_level, normalized_text, warnings, errors = _validation_context(text)

        if validation["level"] == "error":
            result_label = "validation_error"
            return _build_response(
                ok=False,
                level="error",
                category=category,
                normalized_text=normalized_text,
                warnings=[],
                errors=errors,
            )

        cache_key = make_cache_key(normalized_text, target_lang)
        if not force_refresh:
            cached = get_cache(cache_key)
            if cached:
                cached_category = cached.get("category")
                needs_example = cached_category in ("word", "phrase", "sentence")
                if needs_example and not _has_example(cached):
                    # Pre-Qwen cache entries (or fallback-only results) lack an example; re-analyze.
                    delete_cache(cache_key)
                    AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text", result="stale").inc()
                    log_event(
                        logger,
                        logging.INFO,
                        "ai_cache_event",
                        operation="analyze_text",
                        result="stale",
                        category=cached_category,
                    )
                else:
                    cached["cacheHit"] = True
                    AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text", result="hit").inc()
                    log_event(
                        logger,
                        logging.INFO,
                        "ai_cache_event",
                        operation="analyze_text",
                        result="hit",
                        category=cached_category,
                    )
                    return cached
            else:
                AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text", result="miss").inc()
                log_event(
                    logger,
                    logging.INFO,
                    "ai_cache_event",
                    operation="analyze_text",
                    result="miss",
                    category=category,
                )
        else:
            AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text", result="bypass").inc()
            log_event(
                logger,
                logging.INFO,
                "ai_cache_event",
                operation="analyze_text",
                result="bypass",
                category=category,
            )

        # One Ollama call generates the full English-learning reference (meaning, expression
        # type, alternative meanings, usage scenario, example + translation, dialogue,
        # synonyms, similar phrases). word/phrase/sentence all prefer Qwen.
        ollama = None
        if (
            category in ("word", "phrase", "sentence")
            and settings.example_generator_provider == "ollama"
        ):
            if time.monotonic() >= deadline_at:
                result_label = "timeout"
                _ai_event("failure", "AI_TOTAL_TIMEOUT", category=category, stage="before_generate")
                return _build_response(
                    ok=False,
                    level="failed",
                    category=category,
                    normalized_text=normalized_text,
                    warnings=[],
                    errors=[user_message_for("AI_TOTAL_TIMEOUT")],
                    provider=None,
                )
            # One shared monotonic budget across the whole chain: cap the Qwen layer by
            # both its own per-call timeout and the total deadline so queue + generation
            # + parsing + retry together never exceed ai_total_timeout_seconds.
            effective_deadline = min(
                time.monotonic() + settings.ollama_timeout_seconds,
                deadline_at,
            )
            try:
                with observed_operation(
                    "ollama",
                    "qwen_analysis",
                    attributes={"model": settings.ollama_model, "category": category},
                ):
                    ollama = generate_analysis_with_ollama(
                        normalized_text,
                        category,
                        deadline=effective_deadline,
                        attempt_recorder=_make_attempt_recorder(record),
                    )
            except Exception as exc:
                logger.warning("[analyzer] ollama analysis error: %s", _short_error(exc))
                ollama = None

        # Qwen is the formal AI result for word/phrase/sentence. A failed Qwen call must
        # surface as a clear failure, not be disguised by an Argos+template fallback.
        if (
            category in ("word", "phrase", "sentence")
            and settings.example_generator_provider == "ollama"
            and ollama is None
        ):
            if time.monotonic() >= deadline_at:
                code = "AI_TOTAL_TIMEOUT"
                result_label = "timeout"
            else:
                code = "AI_LLM_FAILED"
                result_label = "error"
            _ai_event("failure", code, category=category)
            return _build_response(
                ok=False,
                level="failed",
                category=category,
                normalized_text=normalized_text,
                warnings=[],
                errors=[user_message_for(code)],
                provider=None,
            )

        with observed_operation("ai", "finalize_analysis", attributes={"category": category}):
            return _finalize_analysis(
                category=category,
                cache_key=cache_key,
                validation_level=validation_level,
                normalized_text=normalized_text,
                warnings=warnings,
                errors=errors,
                ollama=ollama,
            )
    except Exception:
        result_label = "exception"
        _ai_event("failure", "AI_INTERNAL_ERROR")
        logger.exception("[analyzer] unexpected failure")
        return _build_response(
            ok=False,
            level="failed",
            category="unknown",
            normalized_text=str(text or "").strip(),
            warnings=[],
            errors=[user_message_for("AI_INTERNAL_ERROR")],
            provider=None,
            cache_hit=False,
        )
    finally:
        record_operation_result(
            "ai",
            "analyze_text_result",
            result_label,
            time.perf_counter() - started,
        )


def analyze_text_streaming(
    text: str,
    card_type: str = "auto",
    target_lang: str = "zh",
    force_refresh: bool = False,
    *,
    deadline_at: float | None = None,
    record: Any | None = None,
    cancel_controller: StreamCancelController | None = None,
):
    """Like ``analyze_text``, but streams Ollama field events as they complete.

    Yields ``("field", key, raw_value)`` for each complete field during the
    single Ollama stream, then a final ``("final", dict)`` carrying the full
    ``AnalyzeResponse`` dict (ok True or False). The ``final`` payload is built
    by the exact same ``_finalize_analysis`` path as the non-streaming endpoint.

    The stream path starts Qwen exactly once and never falls back to a second
    (non-streaming) Qwen call, even if the stream is interrupted.
    """
    started = time.perf_counter()
    result_label = "success"
    if deadline_at is None:
        deadline_at = time.monotonic() + settings.ai_total_timeout_seconds
    _ai_event(
        "deadline_set",
        "started",
        total_timeout_seconds=settings.ai_total_timeout_seconds,
        deadline_at=round(deadline_at, 3),
    )
    try:
        with observed_operation("ai", "validate_english"):
            validation, category, validation_level, normalized_text, warnings, errors = _validation_context(text)

        if validation["level"] == "error":
            result_label = "validation_error"
            yield (
                "final",
                _build_response(
                    ok=False,
                    level="error",
                    category=category,
                    normalized_text=normalized_text,
                    warnings=[],
                    errors=errors,
                ),
            )
            return

        cache_key = make_cache_key(normalized_text, target_lang)
        if not force_refresh:
            cached = get_cache(cache_key)
            if cached:
                cached_category = cached.get("category")
                needs_example = cached_category in ("word", "phrase", "sentence")
                if needs_example and not _has_example(cached):
                    delete_cache(cache_key)
                    AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text_streaming", result="stale").inc()
                    log_event(
                        logger,
                        logging.INFO,
                        "ai_cache_event",
                        operation="analyze_text_streaming",
                        result="stale",
                        category=cached_category,
                    )
                else:
                    cached["cacheHit"] = True
                    AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text_streaming", result="hit").inc()
                    log_event(
                        logger,
                        logging.INFO,
                        "ai_cache_event",
                        operation="analyze_text_streaming",
                        result="hit",
                        category=cached_category,
                    )
                    yield ("final", cached)
                    return
            else:
                AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text_streaming", result="miss").inc()
                log_event(
                    logger,
                    logging.INFO,
                    "ai_cache_event",
                    operation="analyze_text_streaming",
                    result="miss",
                    category=category,
                )
        else:
            AI_CACHE_EVENTS_TOTAL.labels(operation="analyze_text_streaming", result="bypass").inc()
            log_event(
                logger,
                logging.INFO,
                "ai_cache_event",
                operation="analyze_text_streaming",
                result="bypass",
                category=category,
            )

        ollama = None
        if (
            category in ("word", "phrase", "sentence")
            and settings.example_generator_provider == "ollama"
        ):
            if time.monotonic() >= deadline_at:
                result_label = "timeout"
                _ai_event("failure", "AI_TOTAL_TIMEOUT", category=category, stage="before_generate")
                yield (
                    "final",
                    _build_response(
                        ok=False,
                        level="failed",
                        category=category,
                        normalized_text=normalized_text,
                        warnings=[],
                        errors=[user_message_for("AI_TOTAL_TIMEOUT")],
                        provider=None,
                    ),
                )
                return
            effective_deadline = min(
                time.monotonic() + settings.ollama_timeout_seconds,
                deadline_at,
            )
            try:
                with observed_operation(
                    "ollama",
                    "qwen_analysis_stream",
                    attributes={"model": settings.ollama_model, "category": category},
                ):
                    for event in generate_analysis_with_ollama_stream(
                        normalized_text,
                        category,
                        deadline=effective_deadline,
                        attempt_recorder=_make_attempt_recorder(record),
                        cancel_controller=cancel_controller,
                    ):
                        if event[0] == "field":
                            yield event
                        elif event[0] == "cancelled":
                            # The Ollama stream aborted because the client went
                            # away. Propagate a GeneratorExit so the caller (and
                            # the reliability record) classifies this as a
                            # cancellation, never as a 500.
                            raise GeneratorExit
                        else:  # ("result", dict | None)
                            ollama = event[1]
            except Exception as exc:
                logger.warning("[analyzer] ollama analysis error: %s", _short_error(exc))
                ollama = None

        # Qwen is the formal AI result for word/phrase/sentence. A failed Qwen call must
        # surface as a clear failure, not be disguised by an Argos+template fallback.
        if (
            category in ("word", "phrase", "sentence")
            and settings.example_generator_provider == "ollama"
            and ollama is None
        ):
            if time.monotonic() >= deadline_at:
                code = "AI_TOTAL_TIMEOUT"
                result_label = "timeout"
            else:
                code = "AI_LLM_FAILED"
                result_label = "error"
            _ai_event("failure", code, category=category)
            yield (
                "final",
                _build_response(
                    ok=False,
                    level="failed",
                    category=category,
                    normalized_text=normalized_text,
                    warnings=[],
                    errors=[user_message_for(code)],
                    provider=None,
                ),
            )
            return

        yield (
            "final",
            (
                _finalize_analysis(
                    category=category,
                    cache_key=cache_key,
                    validation_level=validation_level,
                    normalized_text=normalized_text,
                    warnings=warnings,
                    errors=errors,
                    ollama=ollama,
                )
            ),
        )
    except GeneratorExit:
        result_label = "cancelled"
        _ai_event("cancelled", "cancelled", stage="client_disconnect")
        raise
    except Exception:
        result_label = "exception"
        _ai_event("failure", "AI_INTERNAL_ERROR")
        logger.exception("[analyzer] unexpected failure")
        yield (
            "final",
            _build_response(
                ok=False,
                level="failed",
                category="unknown",
                normalized_text=str(text or "").strip(),
                warnings=[],
                errors=["分析服务暂时不可用，请稍后重试"],
                provider=None,
                cache_hit=False,
            ),
        )
    finally:
        record_operation_result(
            "ai",
            "analyze_text_streaming_result",
            result_label,
            time.perf_counter() - started,
        )
