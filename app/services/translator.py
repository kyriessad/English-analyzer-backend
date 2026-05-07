"""
翻译流程的中间层,避免主逻辑直接依赖腾讯云 TMT 细节
"""
from typing import Any

from app.providers.tencent_translator import TencentTranslator


def translate_to_zh(text: str) -> dict[str, Any]:
    provider = TencentTranslator.provider
    try:
        translation = TencentTranslator().translate_to_zh(text)
        return {
            "ok": True,
            "translation": translation,
            "provider": provider,
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "translation": None,
            "provider": provider,
            "error": str(exc),
        }
