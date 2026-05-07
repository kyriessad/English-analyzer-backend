"""
真正调用 DeepL 的地方
"""
from typing import Any

from app.core.config import settings

try:
    import deepl
except ImportError:  # pragma: no cover - handled at runtime if dependency is absent
    deepl = None


class DeepLTranslator:
    provider = "deepl"

    def __init__(self, auth_key: str | None = None) -> None:
        self.auth_key = (auth_key if auth_key is not None else settings.deepl_auth_key).strip()

    def translate_to_zh(self, text: str) -> dict[str, Any]:
        if not self.auth_key:
            return {
                "ok": False,
                "translation": None,
                "provider": self.provider,
                "error": "DEEPL_AUTH_KEY is not configured",
            }

        if deepl is None:
            return {
                "ok": False,
                "translation": None,
                "provider": self.provider,
                "error": "deepl package is not installed",
            }

        try:
            translator = deepl.Translator(self.auth_key)
            result = translator.translate_text(
                text,
                source_lang="EN",
                target_lang="ZH-HANS",
            )
            return {
                "ok": True,
                "translation": str(result.text).strip() or None,
                "provider": self.provider,
                "error": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "translation": None,
                "provider": self.provider,
                "error": str(exc),
            }

