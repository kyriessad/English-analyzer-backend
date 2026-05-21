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
（word/phrase）调用 Free Dictionary API 获取真实例句
↓
返回统一结构
"""
from typing import Any

import requests

from app.schemas import Category, Level
from app.services.cache import get_cache, make_cache_key, set_cache
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"
_DICT_API_BASE = "https://api.dictionaryapi.dev/api/v2/entries/en"


def _fetch_dictionary_example(word: str, timeout: int = 5) -> str | None:
    """从 Free Dictionary API 取首条真实英文例句，失败返回 None。"""
    try:
        url = f"{_DICT_API_BASE}/{requests.utils.quote(word.strip())}"
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
        for entry in data:
            for meaning in entry.get("meanings", []):
                for defn in meaning.get("definitions", []):
                    example = str(defn.get("example") or "").strip()
                    if example:
                        return example
        return None
    except Exception:
        return None


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

        # 对单词和短语尝试从词典获取真实例句，并翻译该例句
        example_sentence: str | None = None
        example_translation: str | None = None
        if category in ("word", "phrase") and translation_result.get("ok"):
            example_sentence = _fetch_dictionary_example(normalized_text)
            if example_sentence:
                try:
                    ex_result = translate_to_zh(example_sentence)
                    if ex_result.get("ok"):
                        example_translation = ex_result.get("translation")
                except Exception:
                    pass

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
