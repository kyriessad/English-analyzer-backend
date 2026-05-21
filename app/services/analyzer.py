"""
最核心的主流程文件
用户输入英文
↓
validator.py 检测
↓
如果 error，直接返回
↓
查 cache.py
↓
没有缓存，调用 translator.py 翻译
↓
调用 understanding.py 生成理解
↓
（word/phrase）调用 Hunyuan 生成 AI 例句，失败则 TMT 兜底
↓
返回统一结构
"""
import logging
from typing import Any

from app.schemas import Category, Level
from app.services.cache import get_cache, make_cache_key, set_cache
from app.services.hunyuan_example import generate_example_with_hunyuan
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english

logger = logging.getLogger(__name__)


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"


def _generate_example_with_tmt(
    word: str,
    translation: str,
    category: str,
) -> tuple[str | None, str | None]:
    """
    Fallback example generation using TMT bidirectional translation.
    Builds Chinese sentence templates using the Chinese translation, translates
    zh→en, and returns the first result whose English contains the original word.
    """
    _trans_hint = translation[:40] if translation else ""
    logger.info(
        "[tmt][diag] start | text=%r | category=%s | translation_hint=%r",
        word, category, _trans_hint,
    )
    try:
        from app.providers.tencent_translator import TencentTranslator
        translator = TencentTranslator()

        word_lower = word.lower().strip()
        # Use only the primary segment of the translation (handles "渴望；渴求" → "渴望")
        t = translation.split("；")[0].split(";")[0].split("，")[0].split(",")[0].strip()
        if not t:
            logger.warning(
                "[tmt][diag] fail_reason=tmt_fallback_failed | text=%r | empty primary segment",
                word,
            )
            return None, None

        for zh in [
            f"他非常{t}，每天都如此。",
            f"这让她感到{t}。",
            f"她{t}这件事。",
        ]:
            try:
                en = translator.translate_to_en(zh)
                logger.info(
                    "[tmt][diag] attempt | text=%r | zh=%r | en=%r | match=%s",
                    word, zh, (en or "")[:80], bool(en and word_lower in en.lower()),
                )
                if en and word_lower in en.lower():
                    return en.strip(), zh
            except Exception:
                continue

        logger.warning(
            "[tmt][diag] fail_reason=tmt_fallback_failed | text=%r | all templates failed",
            word,
        )
        return None, None
    except Exception:
        logger.warning(
            "[tmt][diag] fail_reason=tmt_fallback_failed | text=%r | exception in TMT setup",
            word,
        )
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

        # 对单词和短语用 TokenHub Hunyuan 生成 AI 例句
        # translation 用作 Hunyuan prompt 的中文提示，为 None 时 Hunyuan 仍可尝试生成
        example_sentence: str | None = None
        example_translation: str | None = None
        if category in ("word", "phrase"):
            example_sentence, example_translation = generate_example_with_hunyuan(
                normalized_text, translation
            )
            if example_sentence:
                logger.info("[analyzer] Hunyuan SUCCESS for '%s'", normalized_text)
            elif translation:
                # TMT builds Chinese template sentences from translation — skip when unavailable
                logger.info("[analyzer] Hunyuan failed, trying TMT fallback for '%s'", normalized_text)
                example_sentence, example_translation = _generate_example_with_tmt(
                    normalized_text, translation, category
                )
                if example_sentence:
                    logger.info("[analyzer] TMT fallback SUCCESS for '%s'", normalized_text)
                else:
                    logger.info("[analyzer] TMT fallback also failed for '%s'", normalized_text)
            else:
                logger.info("[analyzer] Hunyuan failed, TMT skipped (no translation) for '%s'", normalized_text)

        level: Level = validation_level
        response = _build_response(
            ok=True,
            level=level,
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

        if translation_result.get("ok") and translation:
            set_cache(cache_key, response)

        return response
    except Exception as exc:
        return _build_response(
            ok=False,
            level="failed",
            category="unknown",
            normalized_text=str(text or "").strip(),
            warnings=[],
            errors=[f"分析服务暂时不可用：{exc}"],
            provider=None,
            cache_hit=False,
        )
