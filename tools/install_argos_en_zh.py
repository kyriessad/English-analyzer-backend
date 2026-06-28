"""
Install the Argos Translate en -> zh model idempotently.
"""
from __future__ import annotations

import sys

from argostranslate import package, translate


SAMPLE_TEXT = "I study English every day."


def _get_en_zh_translation():
    installed_languages = translate.get_installed_languages()
    source_language = next(
        (lang for lang in installed_languages if getattr(lang, "code", "") == "en"),
        None,
    )
    target_language = next(
        (lang for lang in installed_languages if getattr(lang, "code", "") == "zh"),
        None,
    )
    if source_language is None or target_language is None:
        return None
    return source_language.get_translation(target_language)


def _validate_translation() -> str:
    translation = _get_en_zh_translation()
    if translation is None:
        raise RuntimeError("Argos en -> zh model is not installed")

    result = str(translation.translate(SAMPLE_TEXT) or "").strip()
    if not result:
        raise RuntimeError("Argos en -> zh validation returned an empty result")
    return result


def main() -> int:
    print("Updating Argos package index...")
    package.update_package_index()

    if _get_en_zh_translation() is not None:
        print("Argos en->zh model 已安装")
        print(f"Validation: {SAMPLE_TEXT} -> {_validate_translation()}")
        return 0

    available_packages = package.get_available_packages()
    model_package = next(
        (
            item
            for item in available_packages
            if getattr(item, "from_code", "") == "en" and getattr(item, "to_code", "") == "zh"
        ),
        None,
    )
    if model_package is None:
        raise RuntimeError("Could not find Argos en -> zh package in the package index")

    print("Downloading Argos en->zh model...")
    model_path = model_package.download()
    print("Installing Argos en->zh model...")
    package.install_from_path(model_path)
    print(f"Validation: {SAMPLE_TEXT} -> {_validate_translation()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Argos en->zh installation failed: {exc}", file=sys.stderr)
        raise
