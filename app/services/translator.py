"""
Translation provider facade.
"""
from typing import Any

from app.core.config import settings


def translate_to_zh(text: str) -> dict[str, Any]:
    provider = settings.translation_provider
    try:
        if provider == "argos":
            from app.providers.argos_translator import ArgosTranslator

            translation = ArgosTranslator().translate_to_zh(text)
        elif provider == "tencent":
            if not settings.enable_tencent_tmt:
                raise RuntimeError("Tencent TMT provider is disabled")
            from app.providers.tencent_translator import TencentTranslator

            translation = TencentTranslator().translate_to_zh(text)
        elif provider == "deepl":
            from app.providers.deepl_translator import DeepLTranslator

            result = DeepLTranslator().translate_to_zh(text)
            translation = result.get("translation") if result.get("ok") else None
            if not translation:
                raise RuntimeError(result.get("error") or "DeepL translation failed")
        else:
            raise RuntimeError(f"Unsupported translation provider: {provider}")

        if not translation:
            raise RuntimeError(f"{provider} returned an empty translation")

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
