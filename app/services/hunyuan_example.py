"""
Generate English example sentences via TokenHub Hunyuan (OpenAI-compatible API).
Failure is always silent and never affects the main analysis flow.
"""
import json
import logging

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


def _text_in_sentence(text: str, sentence: str, *, allow_inflection: bool = False) -> bool:
    """Check whether *text* (or its common inflections) appears in *sentence*.

    When *allow_inflection* is True, common regular inflections are also
    accepted: +s, +es, +d, +ed, +ing, and for words ending in -e the
    forms with dropped -e (e.g. crave → craving, craved).
    """
    lower_text = text.lower()
    lower_sentence = sentence.lower()

    if lower_text in lower_sentence:
        return True

    if not allow_inflection:
        return False

    # Generate common inflected forms
    candidates = {
        lower_text + "s",
        lower_text + "es",
        lower_text + "d",
        lower_text + "ed",
        lower_text + "ing",
    }
    # For words ending in -e, also try dropping -e before adding suffix
    if lower_text.endswith("e") and len(lower_text) > 2:
        stem = lower_text[:-1]
        candidates.update({stem + "ing", stem + "ed", stem + "es", stem + "s", stem + "d"})

    words = lower_sentence.split()
    for w in words:
        w_clean = w.strip(".,!?;:\"'()[]{}")
        if w_clean in candidates:
            return True

    return False


def _call_and_validate(
    text: str,
    chinese_meaning: str | None,
    strict: bool = False,
) -> tuple[str | None, str | None, bool]:
    """
    Make one Hunyuan ChatCompletions call and validate the result.

    Returns (sentence, translation, retry_eligible).
    retry_eligible is True when the sentence was valid but didn't contain
    the original word (or its inflections, when inflection mode is on).
    """
    _mode = "strict" if strict else "loose"
    logger.info(
        "[hunyuan][diag] start | text=%r | mode=%s | has_translation=%s",
        text, _mode, chinese_meaning is not None,
    )

    api_key = (settings.hunyuan_api_key or "").strip()
    base_url = settings.hunyuan_base_url
    model = settings.hunyuan_model

    meaning_hint = f"（含义：{chinese_meaning}）" if chinese_meaning else ""

    if strict:
        system_prompt = (
            "You are an English teacher helping Chinese learners. "
            "Generate a natural English example sentence and its Chinese translation. "
            "You must ONLY return valid JSON, no markdown, no explanation."
        )
        user_prompt = (
            f'Create an example sentence for the English word/phrase "{text}"{meaning_hint}.\n'
            "STRICT requirements:\n"
            f'1. The exampleSentence MUST contain the exact word "{text}". '
            f'Use the base form "{text}" — do NOT use synonyms, '
            f'do NOT use different forms, do NOT use inflections like -ing, -ed, -s\n'
            "2. The example should be concise, daily-life, suitable for English learners\n"
            "3. exampleTranslation must be the natural Chinese translation of the example sentence\n"
            "4. Return ONLY a JSON object (no markdown, no extra text):\n"
            '{"exampleSentence": "...", "exampleTranslation": "..."}'
        )
    else:
        system_prompt = (
            "You are an English teacher helping Chinese learners. "
            "Generate a natural English example sentence and its Chinese translation. "
            "You must ONLY return valid JSON, no markdown, no explanation."
        )
        user_prompt = (
            f'Create an example sentence for the English word/phrase "{text}"{meaning_hint}.\n'
            "Requirements:\n"
            f'1. exampleSentence must be a natural, complete English sentence that contains "{text}" or its inflection\n'
            "2. The example should be concise, daily-life, suitable for English learners\n"
            "3. exampleTranslation must be the natural Chinese translation of the example sentence\n"
            "4. Return ONLY a JSON object (no markdown, no extra text):\n"
            '{"exampleSentence": "...", "exampleTranslation": "..."}'
        )

    url = f"{base_url}/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=15,
        )
    except requests.exceptions.Timeout:
        logger.warning(
            "[hunyuan][diag] fail_reason=model_timeout | text=%r | mode=%s",
            text, _mode,
        )
        return None, None, False

    if resp.status_code != 200:
        error_hint = ""
        try:
            error_body = resp.json()
            error_hint = error_body.get("error", {}).get("message", "") or str(error_body)[:200]
        except Exception:
            error_hint = resp.text[:200]
        logger.warning(
            "[hunyuan][diag] fail_reason=model_api_error | text=%r | mode=%s | HTTP %s: %s",
            text, _mode, resp.status_code, error_hint,
        )
        return None, None, False

    body = resp.json()
    choices = body.get("choices", [])
    if not choices:
        logger.warning(
            "[hunyuan][diag] fail_reason=empty_response | text=%r | mode=%s | no choices",
            text, _mode,
        )
        return None, None, False

    content = str(choices[0].get("message", {}).get("content", "")).strip()
    if not content:
        logger.warning(
            "[hunyuan][diag] fail_reason=empty_response | text=%r | mode=%s | empty content",
            text, _mode,
        )
        return None, None, False

    logger.info(
        "[hunyuan][diag] raw_response(300)=%r | text=%r | mode=%s",
        content[:300], text, _mode,
    )

    # Extract JSON — guard against markdown wrapping
    start = content.find("{")
    end = content.rfind("}") + 1
    if start == -1 or end <= start:
        logger.warning(
            "[hunyuan][diag] fail_reason=json_parse_failed | text=%r | mode=%s | no JSON braces",
            text, _mode,
        )
        return None, None, False

    try:
        data = json.loads(content[start:end])
    except json.JSONDecodeError as e:
        logger.warning(
            "[hunyuan][diag] fail_reason=json_parse_failed | text=%r | mode=%s | %s",
            text, _mode, e,
        )
        return None, None, False

    sentence = str(data.get("exampleSentence") or "").strip()
    translation = str(data.get("exampleTranslation") or "").strip()

    logger.info(
        "[hunyuan][diag] parsed | text=%r | mode=%s | exampleSentence=%r",
        text, _mode, sentence[:120] if sentence else "",
    )

    # Both fields must be present
    if not sentence or not translation:
        logger.warning(
            "[hunyuan][diag] fail_reason=missing_example_sentence | text=%r | mode=%s"
            " | sentence_empty=%s | translation_empty=%s",
            text, _mode, not sentence, not translation,
        )
        return None, None, False
    # Must not be the bare input text itself
    if sentence.lower().strip() == text.lower().strip():
        logger.warning(
            "[hunyuan][diag] fail_reason=exact_match_failed | text=%r | mode=%s"
            " | sentence equals input text",
            text, _mode,
        )
        return None, None, False
    # Must be a real sentence, not a single word
    if len(sentence.split()) < 3:
        logger.warning(
            "[hunyuan][diag] fail_reason=too_few_words | text=%r | mode=%s | sentence=%r",
            text, _mode, sentence,
        )
        return None, None, False
    # Must contain the original input or allowed inflections
    if not _text_in_sentence(text, sentence, allow_inflection=not strict):
        _fail_reason = "exact_match_failed" if strict else "loose_match_failed"
        logger.warning(
            "[hunyuan][diag] fail_reason=%s | text=%r | mode=%s | sentence=%r",
            _fail_reason, text, _mode, sentence[:120],
        )
        return None, None, True

    logger.info("[hunyuan][diag] pass | text=%r | mode=%s | sentence=%r", text, _mode, sentence[:80])
    return sentence, translation, False


def generate_example_with_hunyuan(
    text: str,
    chinese_meaning: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Call TokenHub Hunyuan ChatCompletions to generate an example sentence.

    First attempt requires the exact word. If the sentence does not contain
    the original word, retries once allowing common inflections (+ing, +ed, etc.).
    All other failures fall straight to TMT.

    Returns (exampleSentence, exampleTranslation) or (None, None) on any failure.
    """
    try:
        api_key = (settings.hunyuan_api_key or "").strip()
        if not api_key:
            logger.info("[hunyuan][diag] fail_reason=model_api_error | HUNYUAN_API_KEY not configured")
            return None, None

        logger.info(
            "[hunyuan][diag] entry | text=%r | has_translation=%s | base_url=%s | model=%s",
            text, chinese_meaning is not None,
            settings.hunyuan_base_url, settings.hunyuan_model,
        )

        # First attempt — strict prompt, requires exact word
        sentence, translation, retry_eligible = _call_and_validate(
            text, chinese_meaning, strict=True,
        )
        if sentence is not None:
            return sentence, translation

        # Only retry when the model produced a valid sentence but didn't contain the word
        if not retry_eligible:
            logger.info(
                "[hunyuan][diag] no_retry | text=%r | strict attempt not retry_eligible",
                text,
            )
            return None, None

        logger.info("[hunyuan][diag] retry | text=%r | strict failed with retry_eligible=True", text)
        sentence, translation, _ = _call_and_validate(
            text, chinese_meaning, strict=False,
        )
        if sentence is None:
            logger.warning(
                "[hunyuan][diag] fail_reason=loose_match_failed | text=%r | loose retry also failed",
                text,
            )
        return sentence, translation
    except Exception:
        logger.exception("[hunyuan][diag] fail_reason=model_api_error | unexpected error for %r", text)
        return None, None
