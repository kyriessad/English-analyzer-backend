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
返回统一结构
"""
from typing import Any

from app.schemas import Category, Level
from app.services.cache import get_cache, make_cache_key, set_cache
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"


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
