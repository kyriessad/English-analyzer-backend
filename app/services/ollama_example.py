"""
Generate English example sentences via local Ollama.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from app.core.config import settings
from app.providers.argos_translator import ArgosTranslator
from app.services.hunyuan_example import _text_in_sentence

logger = logging.getLogger(__name__)

OLLAMA_NUM_PREDICT = 220
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class _OllamaDeadlineExpired(Exception):
    pass


@dataclass(frozen=True)
class _AttemptResult:
    sentence: str | None
    translation: str | None
    fail_reason: str | None = None
    retryable: bool = False


def _ollama_url() -> str:
    return f"{settings.ollama_base_url.rstrip('/')}/api/generate"


def _json_schema_format() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "exampleSentence": {"type": "string"},
            "exampleTranslation": {"type": "string"},
        },
        "required": ["exampleSentence", "exampleTranslation"],
    }


def _strip_outer_code_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```") or not text.endswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) < 3:
        return text
    if not lines[0].strip().startswith("```") or lines[-1].strip() != "```":
        return text
    return "\n".join(lines[1:-1]).strip()


def _looks_like_numbered_list(sentence: str) -> bool:
    text = sentence.strip()
    if re.search(r"(^|\n)\s*\d+[\.)]\s+", text):
        return True
    return bool(re.search(r"\n\s*[-*]\s+", text))


def _word_count(sentence: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", sentence))


def _bare_normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text.lower()))


def _is_valid_chinese_translation(text: str | None) -> bool:
    clean_text = str(text or "").strip()
    return bool(clean_text and _CJK_RE.search(clean_text))


def _validate_example(text: str, sentence: str) -> str | None:
    clean_text = str(text or "").strip()
    clean_sentence = str(sentence or "").strip()

    if not clean_sentence:
        return "missing_example_sentence"
    if _bare_normalized(clean_sentence) == _bare_normalized(clean_text):
        return "example_validation_failed"
    if _looks_like_numbered_list(clean_sentence):
        return "example_validation_failed"
    if _word_count(clean_sentence) < 3:
        return "example_validation_failed"
    if not _text_in_sentence(clean_text, clean_sentence, allow_inflection=True):
        return "example_validation_failed"
    return None


def _build_prompts(text: str, chinese_meaning: str | None, *, strict_retry: bool) -> tuple[str, str]:
    target = str(text or "").strip()
    is_phrase = len(target.split()) > 1
    label = "phrase" if is_phrase else "word"
    meaning_hint = f"\nChinese meaning hint: {chinese_meaning}" if chinese_meaning else ""
    stricter = (
        "\nThis is a retry. Be stricter: return only the JSON object, and make sure "
        "exampleSentence contains the target exactly as written."
        if strict_retry
        else ""
    )

    system_prompt = (
        "You are an English teacher helping Chinese learners. "
        "Do not think step by step. Return only valid JSON, no markdown, no notes."
    )
    if is_phrase:
        target_rule = (
            f'The exampleSentence should contain the complete phrase "{target}" as contiguous words. '
            "Do not split the phrase across different parts of the sentence. "
            "If the phrase is an idiom, use its common idiomatic meaning even if the Chinese meaning hint seems literal."
        )
    else:
        target_rule = (
            f'The exampleSentence should contain the word "{target}". '
            "Common inflections are acceptable only when they sound natural."
        )

    user_prompt = (
        f'Create exactly one natural daily-life English example sentence for this {label}: "{target}".'
        f"{meaning_hint}\n"
        "Requirements:\n"
        "1. The sentence must be suitable for English learners.\n"
        "2. The sentence should be 6 to 18 English words.\n"
        "3. The sentence must not be only the target text.\n"
        f"4. {target_rule}\n"
        "5. Treat the Chinese meaning hint as weak disambiguation only; ignore it if it conflicts with natural English usage.\n"
        "6. exampleTranslation must be Simplified Chinese and match the English sentence.\n"
        "7. Return only this JSON shape: "
        '{"exampleSentence": "...", "exampleTranslation": "..."}'
        f"{stricter}"
    )
    return system_prompt, user_prompt


def _build_payload(
    text: str,
    chinese_meaning: str | None,
    *,
    strict_retry: bool,
    format_spec: dict[str, Any] | str,
) -> dict[str, Any]:
    system_prompt, user_prompt = _build_prompts(text, chinese_meaning, strict_retry=strict_retry)
    return {
        "model": settings.ollama_model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "think": settings.ollama_think,
        "format": format_spec,
        "options": {
            "temperature": settings.ollama_temperature,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
        "keep_alive": "5m",
    }


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _OllamaDeadlineExpired
    return remaining


def _post_generate(payload: dict[str, Any], deadline: float) -> requests.Response:
    return requests.post(
        _ollama_url(),
        json=payload,
        timeout=_remaining_timeout(deadline),
    )


def _call_once(
    text: str,
    chinese_meaning: str | None,
    *,
    deadline: float,
    strict_retry: bool = False,
) -> _AttemptResult:
    format_spec: dict[str, Any] | str = _json_schema_format()
    payload = _build_payload(
        text,
        chinese_meaning,
        strict_retry=strict_retry,
        format_spec=format_spec,
    )

    try:
        response = _post_generate(payload, deadline)
        if response.status_code == 400:
            fallback_payload = dict(payload)
            fallback_payload["format"] = "json"
            response = _post_generate(fallback_payload, deadline)
    except _OllamaDeadlineExpired:
        logger.warning("[ollama][diag] fail_reason=ollama_total_timeout")
        return _AttemptResult(None, None, "ollama_total_timeout", False)
    except requests.exceptions.Timeout:
        logger.warning("[ollama][diag] fail_reason=ollama_timeout")
        return _AttemptResult(None, None, "ollama_timeout", False)
    except requests.exceptions.RequestException:
        logger.warning("[ollama][diag] fail_reason=ollama_unavailable")
        return _AttemptResult(None, None, "ollama_unavailable", False)

    if response.status_code != 200:
        logger.warning(
            "[ollama][diag] fail_reason=ollama_http_error | status=%s | body=%r",
            response.status_code,
            response.text[:200],
        )
        return _AttemptResult(None, None, "ollama_http_error", False)

    try:
        body = response.json()
    except ValueError:
        logger.warning("[ollama][diag] fail_reason=json_parse_failed | invalid response body")
        return _AttemptResult(None, None, "json_parse_failed", True)

    content = str(body.get("response") or "").strip()
    if not content:
        logger.warning("[ollama][diag] fail_reason=empty_response")
        return _AttemptResult(None, None, "empty_response", False)

    try:
        data = json.loads(_strip_outer_code_fence(content))
    except json.JSONDecodeError as exc:
        logger.warning("[ollama][diag] fail_reason=json_parse_failed | %s | content=%r", exc, content[:200])
        return _AttemptResult(None, None, "json_parse_failed", True)

    sentence = str(data.get("exampleSentence") or "").strip()
    translation = str(data.get("exampleTranslation") or "").strip()
    if not sentence:
        logger.warning("[ollama][diag] fail_reason=missing_example_sentence")
        return _AttemptResult(None, None, "missing_example_sentence", True)

    validation_error = _validate_example(text, sentence)
    if validation_error:
        logger.warning(
            "[ollama][diag] fail_reason=%s | sentence=%r",
            validation_error,
            sentence[:120],
        )
        return _AttemptResult(None, None, validation_error, True)

    logger.info("[ollama][diag] pass | sentence=%r", sentence[:120])
    return _AttemptResult(sentence, translation or None, None, False)


def generate_example_with_ollama(
    text: str,
    chinese_meaning: str | None = None,
) -> tuple[str | None, str | None]:
    target = str(text or "").strip()
    if not target:
        return None, None

    deadline = time.monotonic() + settings.ollama_timeout_seconds
    first = _call_once(target, chinese_meaning, deadline=deadline, strict_retry=False)
    result = first
    if first.sentence is None and first.retryable:
        logger.info("[ollama][diag] retry | reason=%s", first.fail_reason)
        result = _call_once(target, chinese_meaning, deadline=deadline, strict_retry=True)

    if result.sentence is None:
        return None, None

    if _is_valid_chinese_translation(result.translation):
        return result.sentence, result.translation

    if result.translation:
        logger.warning("[ollama][diag] fail_reason=qwen_example_translation_no_chinese")
    else:
        logger.warning("[ollama][diag] fail_reason=qwen_example_translation_missing")

    fallback_translation = ArgosTranslator().translate_to_zh(result.sentence)
    if not fallback_translation:
        logger.warning("[ollama][diag] fail_reason=argos_example_translation_failed")
    return result.sentence, fallback_translation
