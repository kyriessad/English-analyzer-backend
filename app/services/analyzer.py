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
（word/phrase）调用 Tencent Hunyuan 生成 AI 例句
↓
返回统一结构
"""
import json
from typing import Any

from app.core.config import settings
from app.schemas import Category, Level
from app.services.cache import get_cache, make_cache_key, set_cache
from app.services.translator import translate_to_zh
from app.services.understanding import generate_understanding
from app.services.validator import validate_english


TRANSLATION_UNAVAILABLE_WARNING = "翻译暂时不可用，已先保存英文内容。"

_CATEGORY_CN = {"word": "单词", "phrase": "短语", "sentence": "句子"}


def _generate_example_with_hunyuan(
    word: str,
    translation: str,
    category: str,
) -> tuple[str | None, str | None]:
    """Use Tencent Hunyuan LLM to generate an AI example sentence and Chinese translation."""
    try:
        from tencentcloud.common import credential as tencent_credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.hunyuan.v20230901 import hunyuan_client
        from tencentcloud.hunyuan.v20230901 import models as hunyuan_models

        cred = tencent_credential.Credential(
            settings.tencent_secret_id, settings.tencent_secret_key
        )
        http_profile = HttpProfile()
        http_profile.endpoint = "hunyuan.tencentcloudapi.com"
        http_profile.reqTimeout = 10
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        client = hunyuan_client.HunyuanClient(cred, "", client_profile)

        category_cn = _CATEGORY_CN.get(category, "内容")
        prompt = (
            f'请为英文{category_cn}"{word}"（中文含义：{translation}）'
            f"生成一个自然的英文例句，并提供该例句的中文翻译。\n"
            f"要求：\n"
            f'1. 例句必须自然地包含"{word}"或其变形（过去式、进行时等均可）\n'
            f"2. 例句要简洁、日常、生动易懂\n"
            f"3. 仅返回JSON格式，不添加任何其他内容：\n"
            f'{{"sentence": "...", "translation": "..."}}'
        )

        msg = hunyuan_models.Message()
        msg.Role = "user"
        msg.Content = prompt

        req = hunyuan_models.ChatCompletionsRequest()
        req.Model = "hunyuan-lite"
        req.Messages = [msg]
        req.Stream = False

        resp = client.ChatCompletions(req)
        content = str(resp.Choices[0].Message.Content or "").strip()

        # 提取 JSON，防止 Hunyuan 在 JSON 前后加了说明文字
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            return None, None

        data = json.loads(content[start:end])
        sentence = str(data.get("sentence") or "").strip()
        trans = str(data.get("translation") or "").strip()

        # 验证：例句不能与输入词相同，且必须包含输入词（防止 Hunyuan 不遵守 prompt）
        if not sentence or sentence.lower().strip() == word.lower().strip():
            return None, None
        if word.lower() not in sentence.lower():
            return None, None

        return sentence, trans or None
    except Exception:
        return None, None


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
    try:
        from app.providers.tencent_translator import TencentTranslator
        translator = TencentTranslator()

        word_lower = word.lower().strip()
        # Use only the primary segment of the translation (handles "渴望；渴求" → "渴望")
        t = translation.split("；")[0].split(";")[0].split("，")[0].split(",")[0].strip()
        if not t:
            return None, None

        for zh in [
            f"他非常{t}，每天都如此。",
            f"这让她感到{t}。",
            f"她{t}这件事。",
        ]:
            try:
                en = translator.translate_to_en(zh)
                if en and word_lower in en.lower():
                    return en.strip(), zh
            except Exception:
                continue

        return None, None
    except Exception:
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

        # 对单词和短语用 Tencent Hunyuan 生成 AI 例句
        example_sentence: str | None = None
        example_translation: str | None = None
        if category in ("word", "phrase") and translation:
            example_sentence, example_translation = _generate_example_with_hunyuan(
                normalized_text, translation, category
            )
            if not example_sentence:
                example_sentence, example_translation = _generate_example_with_tmt(
                    normalized_text, translation, category
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
