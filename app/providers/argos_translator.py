"""
Local Argos Translate provider.

The package index and language model are installed by tools/install_argos_en_zh.py.
This module never downloads models during import or request handling.
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

logger = logging.getLogger(__name__)

try:
    from argostranslate import translate as argos_translate
except ImportError:  # pragma: no cover - handled at runtime if dependency is absent
    argos_translate = None


class _Translation(Protocol):
    def translate(self, text: str) -> str:
        ...


class ArgosTranslator:
    provider = "argos"

    _lock = threading.Lock()
    _translation: _Translation | None = None
    _lookup_done = False

    @classmethod
    def reset_cache_for_tests(cls) -> None:
        with cls._lock:
            cls._translation = None
            cls._lookup_done = False

    @classmethod
    def _get_translation(cls) -> _Translation | None:
        if argos_translate is None:
            logger.warning("[argos][diag] fail_reason=package_missing")
            return None

        with cls._lock:
            if cls._lookup_done:
                return cls._translation

            try:
                installed_languages = argos_translate.get_installed_languages()
                source_language = next(
                    (lang for lang in installed_languages if getattr(lang, "code", "") == "en"),
                    None,
                )
                target_language = next(
                    (lang for lang in installed_languages if getattr(lang, "code", "") == "zh"),
                    None,
                )
                if source_language is None or target_language is None:
                    logger.warning(
                        "[argos][diag] fail_reason=model_missing | run tools/install_argos_en_zh.py"
                    )
                    return None

                translation = source_language.get_translation(target_language)
                if translation is None:
                    logger.warning(
                        "[argos][diag] fail_reason=model_missing | run tools/install_argos_en_zh.py"
                    )
                    return None

                cls._translation = translation
                cls._lookup_done = True
                return cls._translation
            except Exception:
                logger.exception("[argos][diag] fail_reason=model_lookup_failed")
                cls._translation = None
                return None

    def translate_to_zh(self, text: str) -> str | None:
        source_text = str(text or "").strip()
        if not source_text:
            return None

        translation = self._get_translation()
        if translation is None:
            return None

        try:
            translated_text = str(translation.translate(source_text) or "").strip()
        except Exception:
            logger.exception("[argos][diag] fail_reason=translate_failed")
            return None

        if not translated_text:
            logger.warning("[argos][diag] fail_reason=empty_translation")
            return None

        return translated_text
