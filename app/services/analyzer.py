"""
Main analysis flow for /api/analyze-english.
"""
import logging
from typing import Any

from app.core.config import settings
from app.schemas import Category, Level
from app.services.cache import delete_cache, get_cache, make_cache_key, set_cache
from app.services.hunyuan_example import generate_example_with_hunyuan
from app.services.ollama_example import generate_example_with_ollama
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english

logger = logging.getLogger(__name__)


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"


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


def _generate_example(
    normalized_text: str,
    category: str,
    translation: str | None,
) -> tuple[str | None, str | None]:
    if not _is_word_or_phrase(category):
        return None, None

    provider = settings.example_generator_provider
    if provider == "ollama":
        return generate_example_with_ollama(normalized_text, translation)

    if provider == "hunyuan":
        if not settings.enable_hunyuan:
            logger.info("[hunyuan][diag] skipped | ENABLE_HUNYUAN=false")
            return None, None

        example_sentence, example_translation = generate_example_with_hunyuan(
            normalized_text,
            translation,
        )
        if example_sentence:
            return example_sentence, example_translation

        if translation and settings.enable_tencent_tmt:
            return _generate_example_with_tmt(normalized_text, translation, category)
        return None, None

    logger.warning("[analyzer] unsupported example generator provider: %s", provider)
    return None, None


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
    }


def analyze_text(
    text: str,
    card_type: str = "auto",
    target_lang: str = "zh",
) -> dict[str, Any]:
    try:
        validation = validate_english(text)
        category = validation["category"]
        validation_level = validation["level"]
        normalized_text = validation["normalizedText"]
        warnings = list(validation.get("warnings") or [])
        errors = list(validation.get("errors") or [])

        if validation["level"] == "error":
            return _build_response(
                ok=False,
                level="error",
                category=category,
                normalized_text=normalized_text,
                warnings=[],
                errors=errors,
            )

        cache_key = make_cache_key(normalized_text, target_lang)
        cached = get_cache(cache_key)
        if cached:
            if _is_word_or_phrase(cached.get("category")) and not _has_example(cached):
                delete_cache(cache_key)
            else:
                cached["cacheHit"] = True
                return cached

        translation_result = translate_to_zh(normalized_text)
        provider = translation_result.get("provider")
        translation = translation_result.get("translation")

        if not translation_result.get("ok") or not translation:
            warnings.append(TRANSLATION_UNAVAILABLE_WARNING)
            translation = None

        understanding = generate_understanding(
            normalized_text,
            category,
            translation,
        )

        example_sentence, example_translation = _generate_example(
            normalized_text,
            category,
            translation,
        )

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
        )

        cacheable = bool(translation_result.get("ok") and translation)
        if cacheable and not (_is_word_or_phrase(category) and not example_sentence):
            set_cache(cache_key, response)

        return response
    except Exception:
        logger.exception("[analyzer] unexpected failure")
        return _build_response(
            ok=False,
            level="failed",
            category="unknown",
            normalized_text=str(text or "").strip(),
            warnings=[],
            errors=["分析服务暂时不可用，请稍后重试"],
            provider=None,
            cache_hit=False,
        )
